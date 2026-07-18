// TactileSight v6 — Haptic grid receiver for STM32 (internal UNO Q MCU)
//
// Protocol: 24-byte binary frames from QRB2210 Linux side at 115200 baud
//   Byte  0:    0x55          sync A
//   Byte  1:    0xAA          sync B
//   Bytes 2-22: cell[0..20]   uint8, 0=silent, 255=max vibration
//   Byte 23:    XOR checksum  (XOR of bytes 2-22)
//
// Grid layout — cell index = row*7 + col:
//   col:   0    1    2    3    4    5    6
//   row 0 [00] [01] [02] [03] [04] [05] [06]   (top)
//   row 1 [07] [08] [09] [10] [11] [12] [13]   (middle)
//   row 2 [14] [15] [16] [17] [18] [19] [20]   (bottom)
//   col 0 = wearer's LEFT side
//
// ── Edit MOTOR_PINS to match your wiring ─────────────────────────────────────
// Use PWM-capable pins. Set to -1 for cells with no motor attached.
const int MOTOR_PINS[21] = {
//  col:   0   1   2   3   4   5   6
          -1, -1, -1, -1, -1, -1, -1,   // row 0 (top)
          -1, -1, -1, -1, -1, -1, -1,   // row 1 (middle)
          -1, -1, -1, -1, -1, -1, -1,   // row 2 (bottom)
};
// ─────────────────────────────────────────────────────────────────────────────

#define BAUD      115200
#define N_CELLS   21
#define SYNC_A    0x55
#define SYNC_B    0xAA
#define FRAME_LEN (2 + N_CELLS + 1)  // 24 bytes total

uint8_t buf[FRAME_LEN];
int     buf_pos = 0;
bool    synced  = false;

void setup() {
    Serial.begin(BAUD);
    for (int i = 0; i < N_CELLS; i++) {
        if (MOTOR_PINS[i] >= 0) {
            pinMode(MOTOR_PINS[i], OUTPUT);
            analogWrite(MOTOR_PINS[i], 0);
        }
    }
}

void apply_grid(uint8_t *cells) {
    for (int i = 0; i < N_CELLS; i++) {
        if (MOTOR_PINS[i] >= 0)
            analogWrite(MOTOR_PINS[i], cells[i]);
    }
}

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
            uint8_t chk = 0;
            for (int i = 2; i < 2 + N_CELLS; i++) chk ^= buf[i];
            if (chk == buf[FRAME_LEN - 1])
                apply_grid(&buf[2]);
            buf_pos = 0;
            synced  = false;
        }
    }
}
