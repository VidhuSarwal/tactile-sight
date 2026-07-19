// =============================================================================
// TactileSight — serial haptic receiver (9x3 ERM band, 27 motors)
// =============================================================================
//
// Reads a 21-cell depth grid over USB serial and drives the band. This is the
// Arduino Router Bridge path's replacement, not its companion: the Bridge never
// registered `set_haptic_grid` on our board, and a hackathon is the wrong place
// to keep debugging someone else's RPC layer. Plain `Serial` is already proven
// on this hardware — the pattern-demo sketch drove all 27 motors through it.
//
// Counterpart: pc-bridge/haptic_serial_bridge.py, running on a laptop.
//
//   laptop  ──HTTP──>  depth server on the camera board  (:8081/grid)
//   laptop  ──USB───>  THIS sketch  ──I2C──>  2x PCA9685  ──>  27 motors
//
// The motor half is VERBATIM from the working pattern demo: Adafruit driver,
// 200 Hz, oscillator 27 MHz, DEADBAND 690 / SATPOINT 1500, motor = col*3 + row.
// Nothing here re-derives it.
//
// ── WIRE FORMAT ──────────────────────────────────────────────────────────────
//
//   0xAA 0x55  <21 bytes, one per cell, 0..255>  <XOR checksum of the 21>
//
// 24 bytes, fixed length, with a two-byte header and a checksum. Framed rather
// than newline-delimited because the payload is raw binary and a 0x0A byte is a
// perfectly legal intensity — a line-based protocol would cut frames in half at
// exactly the intensity that means "something is close".
//
// ── DIAGNOSTICS ──────────────────────────────────────────────────────────────
//
//   * LED matrix sweeps once at boot. No sweep = the sketch is not running.
//   * The matrix then mirrors the grid, brighter = closer.
//   * Top-left pixel toggles on every accepted frame: the "PC is talking to me"
//     light.
//   * Every frame is answered with one ASCII line on serial, so the laptop gets
//     a receipt and can print it. Two-way, and readable in any serial monitor.
//
// =============================================================================

#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include "Arduino_LED_Matrix.h"

// ─── Grid from the laptop ────────────────────────────────────────────────────
#define GRID_COLS 7
#define GRID_ROWS 3
#define N_CELLS   (GRID_COLS * GRID_ROWS)      // 21

// ─── Band geometry (as worn; col 0 = wearer's RIGHT) ─────────────────────────
#define BAND_COLS 9
#define BAND_ROWS 3
#define N_MOTORS  (BAND_COLS * BAND_ROWS)      // 27

// 7 grid columns onto 9 band columns: the outer band columns double up.
static const uint8_t COL_MAP[BAND_COLS] = { 0, 0, 1, 2, 3, 4, 5, 6, 6 };

// ─── Calibration — MEASURED on this harness, from the working demo ───────────
static const uint16_t DEADBAND = 690;    // below this the motor does not turn
static const uint16_t SATPOINT = 1500;   // above this it gets no stronger
#define MIN_LEVEL   20                   // grid level below this = off
#define FAILSAFE_MS 1000                 // no frame for this long = all off
#define BAUD        115200

// ─── Protocol ────────────────────────────────────────────────────────────────
#define HDR0 0xAA
#define HDR1 0x55

Adafruit_PWMServoDriver pca1(0x40);
Adafruit_PWMServoDriver pca2(0x60);
Arduino_LED_Matrix matrix;

static uint8_t  grid[N_CELLS];
static uint8_t  current_level[N_MOTORS];
static uint8_t  canvas[8][13];
static bool     rx_blink = false;
static uint32_t last_rx_ms = 0;
static uint32_t frames_ok = 0, frames_bad = 0;

// ─── Motors: identical to the proven demo ────────────────────────────────────
static inline uint8_t motorIndex(uint8_t col, uint8_t row) { return col * 3 + row; }

static void motorOn(uint8_t idx, uint16_t duty) {
    if (idx >= N_MOTORS) return;
    if (duty > 4095) duty = 4095;
    if (idx < 14) pca1.setPWM(idx, 0, duty);
    else          pca2.setPWM(idx - 14, 0, duty);
}

// level 0..255 -> the motor's usable window. 0 is hard off, not DEADBAND.
static void motorLevel(uint8_t idx, uint8_t level) {
    if (level < MIN_LEVEL) { motorOn(idx, 0); return; }
    uint16_t duty = DEADBAND + (uint16_t)(((uint32_t)(SATPOINT - DEADBAND) * level) / 255UL);
    motorOn(idx, duty);
}

static void allOff() {
    for (uint8_t i = 0; i < N_MOTORS; i++) { motorOn(i, 0); current_level[i] = 0; }
}

// The 9x3 band, rebuilt from the 7-column grid. THIS is what the wearer feels,
// and it is what both the motors and the LED matrix render from - so the panel
// can never disagree with the skin. It used to: the motors ran off this map
// while the matrix upscaled the raw 7-column grid, so the two outer columns
// looked single-width on the panel while two motors each were buzzing.
static uint8_t band[N_MOTORS];

static void buildBand() {
    for (uint8_t c = 0; c < BAND_COLS; c++) {
        uint8_t gc = COL_MAP[c];
        for (uint8_t r = 0; r < BAND_ROWS; r++) {
            uint8_t v = grid[r * GRID_COLS + gc];
            band[motorIndex(c, r)] = (v < MIN_LEVEL) ? 0 : v;
        }
    }
}

// Returns how many motors are being driven, for the receipt.
static uint8_t driveMotors() {
    uint8_t active = 0;
    for (uint8_t m = 0; m < N_MOTORS; m++) {
        if (band[m] != current_level[m]) {          // only write what changed
            motorLevel(m, band[m]);
            current_level[m] = band[m];
        }
        if (band[m]) active++;
    }
    return active;
}

// ─── Matrix ──────────────────────────────────────────────────────────────────
static void renderMatrix() {
    bool anything = false;

    for (uint8_t r = 0; r < 8; r++) {
        for (uint8_t c = 0; c < 13; c++) {
            // Upscale the 9x3 BAND onto the 13x8 panel, so what you see is
            // exactly what the motors are doing - doubled outer columns and
            // all. Centre sampling rather than truncation: (c * 9 / 13) biases
            // every cell left and starves the last column, which made the
            // right edge of the band read narrower than it is.
            uint8_t bc = (uint8_t)(((2u * c + 1u) * BAND_COLS) / 26u);   // /(2*13)
            uint8_t br = (uint8_t)(((2u * r + 1u) * BAND_ROWS) / 16u);   // /(2*8)
            uint8_t v  = band[motorIndex(bc, br)];

            if (v < MIN_LEVEL) {
                canvas[r][c] = 0;
            } else {
                // Map MIN_LEVEL..255 onto 1..7, never 0. The old (v * 7) / 255
                // truncated everything below 37 to zero, so a cell that was ON
                // rendered as black - the matrix said "clear" while a motor was
                // buzzing. Anything above the threshold must be visible.
                canvas[r][c] = (uint8_t)(1u + ((uint32_t)(v - MIN_LEVEL) * 6u)
                                              / (255u - MIN_LEVEL));
                anything = true;
            }
        }
    }

    // Heartbeat only when the band is idle, so it never overwrites real data.
    // Dark panel + blinking corner = link alive, nothing in range. Dark panel
    // with no blink = the link is gone.
    if (!anything) canvas[0][0] = rx_blink ? 3 : 0;

    matrix.renderBitmap(canvas, 8, 13);
}

static void sweep() {
    for (int c = 0; c < 13; c++) {
        for (int r = 0; r < 8; r++) canvas[r][c] = 6;
        matrix.renderBitmap(canvas, 8, 13);
        delay(25);
    }
    for (int c = 0; c < 13; c++) {
        for (int r = 0; r < 8; r++) canvas[r][c] = 0;
        matrix.renderBitmap(canvas, 8, 13);
        delay(25);
    }
}

// ─── Frame reader ────────────────────────────────────────────────────────────
// A byte-at-a-time state machine rather than a blocking read: loop() must keep
// running so the failsafe still fires if the laptop goes away mid-frame.
static uint8_t  buf[N_CELLS];
static uint8_t  state = 0;      // 0 = want HDR0, 1 = want HDR1, 2 = payload, 3 = checksum
static uint8_t  filled = 0;

static void handleByte(uint8_t b) {
    switch (state) {
        case 0:
            if (b == HDR0) state = 1;
            break;
        case 1:
            // Not 0x55: this might itself be a fresh HDR0, so do not drop it.
            if (b == HDR1) { state = 2; filled = 0; }
            else if (b != HDR0) state = 0;
            break;
        case 2:
            buf[filled++] = b;
            if (filled >= N_CELLS) state = 3;
            break;
        case 3: {
            uint8_t sum = 0;
            for (uint8_t i = 0; i < N_CELLS; i++) sum ^= buf[i];
            if (sum == b) {
                memcpy(grid, buf, N_CELLS);
                last_rx_ms = millis();
                rx_blink = !rx_blink;
                frames_ok++;
                buildBand();                  // one source of truth for both
                uint8_t active = driveMotors();
                renderMatrix();
                // The receipt. One line per frame, so the laptop can prove the
                // band is receiving without anyone watching the hardware.
                Serial.print("OK ");
                Serial.print(frames_ok);
                Serial.print(" motors=");
                Serial.println(active);
            } else {
                frames_bad++;
                Serial.print("BAD checksum n=");
                Serial.println(frames_bad);
            }
            state = 0;
            break;
        }
    }
}

void setup() {
    memset(grid, 0, sizeof(grid));
    memset(canvas, 0, sizeof(canvas));
    memset(current_level, 0, sizeof(current_level));

    Serial.begin(BAUD);

    matrix.begin();
    matrix.setGrayscaleBits(3);
    sweep();                       // proof of life before anything can block

    Wire.begin();
    pca1.begin();
    pca1.setOscillatorFrequency(27000000);
    pca1.setPWMFreq(200);
    pca2.begin();
    pca2.setOscillatorFrequency(27000000);
    pca2.setPWMFreq(200);
    allOff();

    Serial.println("READY TactileSight serial receiver 9x3");
    last_rx_ms = millis();
}

void loop() {
    while (Serial.available()) handleByte((uint8_t)Serial.read());

    // A wearable that buzzes forever because the laptop died is worse than one
    // that goes quiet.
    if ((uint32_t)(millis() - last_rx_ms) > FAILSAFE_MS) {
        bool anything = false;
        for (uint8_t i = 0; i < N_MOTORS; i++) if (current_level[i]) { anything = true; break; }
        if (anything) {
            memset(grid, 0, sizeof(grid));
            memset(band, 0, sizeof(band));
            allOff();
            renderMatrix();
            Serial.println("FAILSAFE no data");
        }
        last_rx_ms = millis();
    }
}
