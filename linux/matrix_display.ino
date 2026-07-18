// TactileSight — LED Matrix Display (Arduino UNO R4 WiFi, 12×8 built-in matrix)
//
// Standalone sketch — completely independent of tactile_receiver.ino.
// Reads the same 24-byte haptic packets from the QRB2210 Linux side:
//   0x55 0xAA [21 bytes: cell0..cell20, uint8 0–255] [XOR checksum]
//
// Maps the 7×3 haptic grid → 12×8 LED matrix, scaled to fill the whole display.
// High depth value (obstacle close) = more LEDs lit.
// Low value (open/far) = LEDs off.
//
// Board:   Arduino UNO R4 WiFi
// Library: Arduino_LED_Matrix  (pre-installed with the R4 board package)
// Serial:  hardware Serial at 115200 baud (same port used by Linux UART)

#include "Arduino_LED_Matrix.h"

// ── Config ──────────────────────────────────────────────────────────────────
#define BAUD         115200
#define N_CELLS      21         // 7 cols × 3 rows
#define SYNC_A       0x55
#define SYNC_B       0xAA
#define FRAME_LEN    (2 + N_CELLS + 1)  // 24 bytes total
#define THRESHOLD    40         // cell value above this = LED on (0–255 scale)
                                // lower = more sensitive; raise to reduce noise
// ─────────────────────────────────────────────────────────────────────────────

ArduinoLEDMatrix matrix;

// 8 rows × 12 cols — this is the format renderBitmap() expects
uint8_t frame[8][12];

// Receive buffer
uint8_t buf[FRAME_LEN];
int     buf_pos = 0;
bool    synced  = false;

// Current haptic grid (21 cells, 0–255)
uint8_t haptic[N_CELLS] = {0};

// ── Render haptic[21] → frame[8][12] ────────────────────────────────────────
// Each LED (led_row, led_col) is mapped back to the haptic cell that covers
// that fraction of the display:
//   haptic_col = (led_col * 7) / 12   → 0..6
//   haptic_row = (led_row * 3) / 8    → 0..2
//   haptic_idx = haptic_row * 7 + haptic_col
void render() {
    for (int lr = 0; lr < 8; lr++) {
        for (int lc = 0; lc < 12; lc++) {
            int hc  = (lc * 7) / 12;
            int hr  = (lr * 3) / 8;
            int idx = hr * 7 + hc;
            frame[lr][lc] = (haptic[idx] > THRESHOLD) ? 1 : 0;
        }
    }
    matrix.renderBitmap(frame, 8, 12);
}

// ── Startup animation — wipe on, hold, wipe off ──────────────────────────────
void startup_animation() {
    // wipe right
    for (int lc = 0; lc < 12; lc++) {
        for (int lr = 0; lr < 8; lr++) frame[lr][lc] = 1;
        matrix.renderBitmap(frame, 8, 12);
        delay(40);
    }
    delay(300);
    // wipe left
    for (int lc = 11; lc >= 0; lc--) {
        for (int lr = 0; lr < 8; lr++) frame[lr][lc] = 0;
        matrix.renderBitmap(frame, 8, 12);
        delay(40);
    }
}

void setup() {
    matrix.begin();
    memset(frame, 0, sizeof(frame));
    startup_animation();
    Serial.begin(BAUD);
}

// ── Packet parser ────────────────────────────────────────────────────────────
void loop() {
    while (Serial.available()) {
        uint8_t b = (uint8_t)Serial.read();

        if (!synced) {
            if (buf_pos == 0 && b == SYNC_A) {
                buf[buf_pos++] = b;
            } else if (buf_pos == 1 && b == SYNC_B) {
                buf[buf_pos++] = b;
                synced = true;
            } else {
                buf_pos = 0;
            }
            continue;
        }

        buf[buf_pos++] = b;

        if (buf_pos == FRAME_LEN) {
            // Validate XOR checksum
            uint8_t chk = 0;
            for (int i = 2; i < 2 + N_CELLS; i++) chk ^= buf[i];

            if (chk == buf[FRAME_LEN - 1]) {
                memcpy(haptic, &buf[2], N_CELLS);
                render();
            }

            buf_pos = 0;
            synced  = false;
        }
    }
}
