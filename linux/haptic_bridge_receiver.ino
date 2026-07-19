// TactileSight — STM32U585 haptic receiver: PCA9685 motors + on-board LED matrix
//
// Receives the 21-cell haptic grid (7 cols x 3 rows) from the QRB2210 Linux side
// over the Arduino Router Bridge, then:
//   1. drives the band's vibration motors via two PCA9685 boards, and
//   2. mirrors the obstacle picture onto the UNO Q's 13x8 LED matrix, so the
//      board shows the same thing the web UI does.
//
// Counterpart: linux/bridge_sender.py -> Bridge.call("set_haptic_grid", [c0..c20])
//              (ONE msgpack array argument, not 21 positional args)
//
// Board: Arduino UNO Q (arduino:zephyr:unoq)
// Build: arduino-cli compile -b arduino:zephyr:unoq <sketchdir>
// Flash: arduino-cli upload  -b arduino:zephyr:unoq -p 10.89.1.1 <sketchdir>
//
// UNO Q sketch rules (stm.md): never call Serial1.begin() (reserved by the
// bridge); use Monitor.println() for debug; keep Bridge handlers fast.

#include <Wire.h>
#include "Arduino_LED_Matrix.h"
#include "Arduino_RouterBridge.h"

// ── Incoming grid geometry (fixed by the Linux side) ─────────────────────────
#define GRID_COLS   7
#define GRID_ROWS   3
#define N_CELLS     (GRID_COLS * GRID_ROWS)   // 21;  index = row * 7 + col
                                              // col 0 = wearer's LEFT, row 0 = top

// ── Harness geometry ─────────────────────────────────────────────────────────
// The harness is 3 rows x 9 columns = 27 motors, but the depth grid is only
// 7 columns wide. The two extra physical columns are the outermost left/right
// positions, which have no depth data of their own, so they repeat their
// neighbour:
//
//   phys col:  0  1  2  3  4  5  6  7  8
//   grid col:  0  0  1  2  3  4  5  6  6
//              ^^                    ^^
//              extra left            extra right
//
// Edit COL_MAP if the outer columns should behave differently.
#define BAND_COLS   9
#define BAND_ROWS   3
#define N_MOTORS    (BAND_COLS * BAND_ROWS)   // 27
static const uint8_t COL_MAP[BAND_COLS] = { 0, 0, 1, 2, 3, 4, 5, 6, 6 };

// Motor numbering is COLUMN-MAJOR, as wired:
//   motor 0 = col0/row0, motor 1 = col0/row1, motor 2 = col0/row2,
//   motor 3 = col1/row0, ...      i.e.  motor = col * 3 + row
// Motors 0..13  -> PCA #1 (0x40) channels 0..13
// Motors 14..26 -> PCA #2 (0x60) channels 0..12   (continues the same series)
// With 27 motors, PCA #2 uses channels 0..12; channels 13-15 are spare.
#define PCA1_ADDR       0x40
#define PCA2_ADDR       0x60
#define PCA1_MOTORS     14

// ── Tuning ───────────────────────────────────────────────────────────────────
#define PWM_FREQ_HZ     1000   // vibration motors like ~1kHz
#define MIN_LEVEL       20     // below this -> off (kills noise-buzz)
#define MAX_LEVEL       255    // cap continuous drive against the skin
#define FAILSAFE_MS     1500   // no grid for this long -> all motors off
#define MATRIX_GAMMA_ON 1      // 1 = perceptual curve on the LED matrix

// ── Motor PWM calibration (MEASURED on this harness) ─────────────────────────
// These motors do NOT use the PCA9685's full 0-4095 range:
//   * below ~690 counts the motor does not turn at all (dead band), and
//   * above ~1300-1500 counts the vibration stops getting stronger (saturation).
// So the entire useful range is 690..~1400 — about 17% of the chip's span.
//
// Driving 0-4095 linearly would therefore be wrong twice over: every level
// below 43/255 would land inside the dead band and feel like nothing, and
// everything above ~87/255 would feel identical while burning ~3x the current
// for no extra sensation.
//
// Instead we map level 1..255 onto PWM_MIN..PWM_MAX so the whole 0-255 range
// the Linux side sends is actually perceptible. Level 0 stays hard off.
// Re-measure and adjust these two numbers if the motors or supply change.
#define PWM_MIN         690    // dead-band threshold: below this, no movement
#define PWM_MAX         1400   // saturation: above this, no extra vibration

// ── Power management ─────────────────────────────────────────────────────────
// Capping duty at PWM_MAX (~34% of full scale) already cuts average current to
// roughly a third, but it does NOT remove the startup transient: a motor
// breaking static friction still pulls several times its running current for
// the first few ms. All 27 doing that on the same millisecond would sag the
// rail and can reset the board. Two independent limits stop that, and neither
// adds meaningful latency to an obstacle alert:
//
//   1. MAX_ACTIVE_MOTORS — never run more than this many at once. When more
//      cells are hot, keep the STRONGEST ones (nearest obstacles) and drop the
//      rest. This bounds steady-state current AND is better to wear: 27 motors
//      buzzing at once tells you nothing, the nearest few tell you where to go.
//
//   2. STAGGER_US — when several motors switch ON in the same frame, space
//      their starts out so the inrush peaks land at different moments.
//      Worst case added delay = MAX_ACTIVE_MOTORS * STAGGER_US
//      = 10 * 1500us = 15 ms, i.e. under half of one 30fps frame (33 ms), so
//      it is not perceptible in an alert. Motors already running are updated
//      immediately with no stagger — only new starts pay the cost.
//
// Raise MAX_ACTIVE_MOTORS only if the supply can genuinely take it.
#define MAX_ACTIVE_MOTORS 10
#define STAGGER_US        1500

// ── PCA9685 registers ────────────────────────────────────────────────────────
#define PCA_MODE1        0x00
#define PCA_MODE2        0x01
#define PCA_LED0_ON_L    0x06
#define PCA_PRESCALE     0xFE
#define PCA_MODE1_SLEEP  0x10
#define PCA_MODE1_AI     0x20   // auto-increment
#define PCA_MODE2_OUTDRV 0x04   // totem-pole output

Arduino_LED_Matrix matrix;

static uint8_t  grid[N_CELLS];        // latest grid from Linux, 0..255
static uint8_t  canvas[8][13];        // LED matrix framebuffer, 0..7 grayscale
static volatile uint32_t last_rx_ms;  // for the failsafe
static volatile bool     grid_dirty;
static bool     pca_ok[2];

// ── PCA9685 driver (inline; avoids a library dependency on the Zephyr core) ──
static void pcaWrite8(uint8_t addr, uint8_t reg, uint8_t val) {
    Wire.beginTransmission(addr);
    Wire.write(reg);
    Wire.write(val);
    Wire.endTransmission();
}

static bool pcaPresent(uint8_t addr) {
    Wire.beginTransmission(addr);
    return Wire.endTransmission() == 0;
}

static bool pcaInit(uint8_t addr) {
    if (!pcaPresent(addr)) return false;
    // Sleep, set prescale, wake, enable auto-increment.
    uint8_t prescale = (uint8_t)(25000000.0 / (4096.0 * PWM_FREQ_HZ) - 1.0 + 0.5);
    pcaWrite8(addr, PCA_MODE1, PCA_MODE1_SLEEP);
    pcaWrite8(addr, PCA_PRESCALE, prescale);
    pcaWrite8(addr, PCA_MODE1, 0x00);
    delay(5);                                   // oscillator restart
    pcaWrite8(addr, PCA_MODE1, PCA_MODE1_AI);
    pcaWrite8(addr, PCA_MODE2, PCA_MODE2_OUTDRV);
    return true;
}

// Set one channel to a 12-bit duty (0..4095), using the full-off/full-on bits
// at the extremes so the motor is genuinely idle at 0.
static void pcaSetPWM(uint8_t addr, uint8_t ch, uint16_t duty) {
    uint16_t on = 0, off = duty;
    if (duty == 0)         { on = 0;      off = 0x1000; }   // full OFF
    else if (duty >= 4095) { on = 0x1000; off = 0;      }   // full ON
    Wire.beginTransmission(addr);
    Wire.write(PCA_LED0_ON_L + 4 * ch);
    Wire.write(on  & 0xFF); Wire.write(on  >> 8);
    Wire.write(off & 0xFF); Wire.write(off >> 8);
    Wire.endTransmission();
}

// motor index -> (board address, channel, bank)
static inline void motorTarget(uint8_t m, uint8_t *addr, uint8_t *ch, uint8_t *bank) {
    if (m < PCA1_MOTORS) { *addr = PCA1_ADDR; *ch = m;               *bank = 0; }
    else                 { *addr = PCA2_ADDR; *ch = m - PCA1_MOTORS; *bank = 1; }
}

static void setMotor(uint8_t m, uint8_t level) {
    uint8_t addr, ch, bank;
    motorTarget(m, &addr, &ch, &bank);
    if (!pca_ok[bank]) return;
    // Map 1..255 onto the motor's usable PWM window (see PWM_MIN/PWM_MAX).
    // 0 is hard off, not PWM_MIN, so idle really means idle.
    uint16_t duty = (level == 0)
        ? 0
        : (uint16_t)(PWM_MIN + ((uint32_t)level * (PWM_MAX - PWM_MIN)) / 255UL);
    pcaSetPWM(addr, ch, duty);
}

static uint8_t current_level[N_MOTORS];   // what each motor is actually set to

// Turning motors OFF draws no current, so this needs no stagger or budget.
static void allMotorsOff() {
    for (uint8_t m = 0; m < N_MOTORS; m++) {
        setMotor(m, 0);
        current_level[m] = 0;
    }
}

// ── Grid -> motors ───────────────────────────────────────────────────────────
static void driveMotors() {
    uint8_t target[N_MOTORS];

    // 1. Map the 7-column grid onto the 9-column harness (column-major wiring).
    for (uint8_t c = 0; c < BAND_COLS; c++) {
        uint8_t gc = COL_MAP[c];
        for (uint8_t r = 0; r < BAND_ROWS; r++) {
            uint8_t v = grid[r * GRID_COLS + gc];
            if (v < MIN_LEVEL) v = 0;
            if (v > MAX_LEVEL) v = MAX_LEVEL;
            target[c * BAND_ROWS + r] = v;
        }
    }

    // 2. Power budget: keep only the strongest MAX_ACTIVE_MOTORS.
    // Rather than sort 27 entries every frame, find a cutoff level by counting
    // how many motors sit at or above each level, walking down from the top.
    uint8_t active = 0;
    for (uint8_t m = 0; m < N_MOTORS; m++) if (target[m]) active++;

    if (active > MAX_ACTIVE_MOTORS) {
        uint8_t cutoff = 255;
        while (cutoff > MIN_LEVEL) {
            uint8_t n = 0;
            for (uint8_t m = 0; m < N_MOTORS; m++) if (target[m] >= cutoff) n++;
            if (n >= MAX_ACTIVE_MOTORS) break;
            cutoff--;
        }
        // Drop everything below the cutoff, and trim any excess at the cutoff
        // itself (ties) so we never exceed the budget.
        uint8_t kept = 0;
        for (uint8_t m = 0; m < N_MOTORS; m++) {
            if (target[m] < cutoff || kept >= MAX_ACTIVE_MOTORS) target[m] = 0;
            else kept++;
        }
    }

    // 3. Write only what changed, staggering NEW starts to spread the inrush.
    // Skipping unchanged channels also keeps I2C traffic down, which matters:
    // rewriting all 27 channels every frame would itself cost several ms.
    bool first_start = true;
    for (uint8_t m = 0; m < N_MOTORS; m++) {
        if (target[m] == current_level[m]) continue;

        bool starting = (current_level[m] == 0 && target[m] > 0);
        if (starting) {
            if (!first_start) delayMicroseconds(STAGGER_US);
            first_start = false;
        }
        setMotor(m, target[m]);
        current_level[m] = target[m];
    }
}

// ── Grid -> 13x8 LED matrix ──────────────────────────────────────────────────
// Same picture as the web UI: brighter = closer. Nearest-neighbour upscale of
// the 7x3 grid onto the 13x8 panel, in 8 grayscale levels.
static const uint8_t GAMMA8[8] = { 0, 1, 1, 2, 3, 4, 5, 7 };

static void renderMatrix() {
    for (uint8_t r = 0; r < 8; r++) {
        for (uint8_t c = 0; c < 13; c++) {
            uint8_t gc = (uint8_t)((c * GRID_COLS) / 13);   // 0..6
            uint8_t gr = (uint8_t)((r * GRID_ROWS) / 8);    // 0..2
            uint8_t v  = grid[gr * GRID_COLS + gc];
            uint8_t lv = (v < MIN_LEVEL) ? 0 : (uint8_t)((v * 7UL) / 255UL);
#if MATRIX_GAMMA_ON
            lv = GAMMA8[lv & 0x07];
#endif
            canvas[r][c] = lv;
        }
    }
    matrix.renderBitmap(canvas, 8, 13);
}

// ── Bridge handler ───────────────────────────────────────────────────────────
// One msgpack array of 21 bytes. Kept deliberately tiny: copy and flag, and let
// loop() do the I2C and matrix work (handlers must not block).
void set_haptic_grid(MsgPack::arr_t<uint8_t> cells) {
    size_t n = cells.size() < (size_t)N_CELLS ? cells.size() : (size_t)N_CELLS;
    for (size_t i = 0; i < n; i++) grid[i] = cells[i];
    for (size_t i = n; i < (size_t)N_CELLS; i++) grid[i] = 0;
    last_rx_ms = millis();
    grid_dirty = true;
}

// ── Startup sweep so you can see the panel and motors are alive ──────────────
static void startupAnimation() {
    for (int c = 0; c < 13; c++) {
        for (int r = 0; r < 8; r++) canvas[r][c] = 5;
        matrix.renderBitmap(canvas, 8, 13);
        delay(25);
    }
    for (int c = 0; c < 13; c++) {
        for (int r = 0; r < 8; r++) canvas[r][c] = 0;
        matrix.renderBitmap(canvas, 8, 13);
        delay(25);
    }
}

void setup() {
    memset(grid, 0, sizeof(grid));
    memset(canvas, 0, sizeof(canvas));

    Wire.begin();
    Wire.setClock(400000);           // fast-mode I2C; keeps 27-channel updates quick
    memset(current_level, 0, sizeof(current_level));
    matrix.begin();
    matrix.setGrayscaleBits(3);      // 8 levels, 0..7
    Monitor.begin();

    pca_ok[0] = pcaInit(PCA1_ADDR);
    pca_ok[1] = pcaInit(PCA2_ADDR);
    allMotorsOff();

    startupAnimation();

    Bridge.begin();
    bool bound = Bridge.provide_safe("set_haptic_grid", set_haptic_grid);

    Monitor.println("TactileSight band receiver");
    Monitor.print("  PCA 0x40: "); Monitor.println(pca_ok[0] ? "OK" : "NOT FOUND");
    Monitor.print("  PCA 0x60: "); Monitor.println(pca_ok[1] ? "OK" : "NOT FOUND");
    Monitor.print("  set_haptic_grid bound: "); Monitor.println(bound ? "yes" : "NO");
    last_rx_ms = millis();
}

void loop() {
    Bridge.update();

    if (grid_dirty) {
        grid_dirty = false;
        driveMotors();
        renderMatrix();
    }

    // Failsafe: if Linux stops sending, stop vibrating. A wearable that buzzes
    // forever because the host died is worse than one that goes quiet.
    if ((uint32_t)(millis() - last_rx_ms) > FAILSAFE_MS) {
        memset(grid, 0, sizeof(grid));
        allMotorsOff();
        renderMatrix();
        last_rx_ms = millis();
    }
}
