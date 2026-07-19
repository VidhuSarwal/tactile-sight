# TactileSight — Linux↔STM32 Communication Guide

This guide covers how the QRB2210 Linux side and the internal STM32U585 communicate on the
Arduino UNO Q, what is reserved by the system, and how to send the haptic grid from Python
to an Arduino sketch running on the STM32.

---

## Architecture

The Arduino UNO Q has two processors on a single board:

| Processor | Role | OS |
|-----------|------|----|
| Qualcomm QRB2210 (arm64) | Linux host — runs the depth server, Python code | Debian 13 |
| STM32U585 (Cortex-M33) | MCU — runs Arduino sketches, drives GPIO/PWM | Zephyr via Arduino |

They communicate over an internal high-speed UART managed by the **Arduino Router Bridge**
system. This bridge is **not a raw serial port you can open yourself** — it uses a
MessagePack RPC protocol and runs as a system daemon.

---

## What is RESERVED — do not touch

| Resource | Who owns it | What breaks if you open it |
|----------|-------------|---------------------------|
| `/dev/ttyHS1` (Linux) | `arduino-router` daemon | Bridge crashes, Python Bridge.call() stops working |
| `Serial1` (Arduino sketch) | `Arduino_RouterBridge` library | Same — corrupts the bridge framing |

**Never call `serial.Serial('/dev/ttyHS1', ...)` in Python.**
**Never call `Serial1.begin()` in a sketch.**

Opening either of these from user code causes the inter-processor communication daemon to
lose sync, which requires a board reboot to recover.

---

## Correct Communication Path: Arduino Bridge / RPC

The bridge daemon exposes a Unix domain socket at `/var/run/arduino-router.sock` using the
**MessagePack RPC** protocol. The Python `arduino.app_utils` library wraps this socket.

Architecture:
```
Python (QRB2210)                      STM32 sketch
     │                                     │
     │  Bridge.call("set_haptic", v0..v20) │
     │  ──── MsgPack RPC ──────────────►   │
     │                                     │  handler runs, drives PWM
     │  ◄──── return value ────────────    │
```

**Key constraint**: The Python side always initiates calls. The STM32 cannot push data to
Linux on its own — it can only respond to a `Bridge.call()`.

---

## Python Side (QRB2210 / Linux)

### Install the bridge library

The `arduino.app_utils` package is pre-installed on the UNO Q. Verify:

```bash
python3 -c "from arduino.app_utils import *; print('ok')"
```

If missing, it ships with the Arduino App Lab SDK — reinstall from the Arduino UNO Q
documentation.

### Calling a function on the STM32

```python
from arduino.app_utils import *

# Call a function registered in the Arduino sketch
result = Bridge.call("function_name", arg0, arg1, ...)
```

### Sending the haptic grid at 30fps

```python
from arduino.app_utils import *
import time

while True:
    # Read the 21-cell grid from shared memory (written by depth server at 30fps)
    try:
        grid = list(open('/dev/shm/tactile/haptic_grid.bin', 'rb').read())
    except FileNotFoundError:
        grid = [0] * 21

    # Push to STM32 — function must be registered in the Arduino sketch
    try:
        Bridge.call("set_haptic_grid",
                    *grid)   # expands to 21 positional args
    except Exception as e:
        print(f"bridge error: {e}")

    time.sleep(1 / 30)
```

The bridge socket at `/var/run/arduino-router.sock` is confirmed live on the board.

### Alternatively: direct MsgPack socket

If you prefer not to use the Python bridge library, you can write MsgPack RPC frames
directly to the Unix socket:

```python
import socket, msgpack

SOCK = "/var/run/arduino-router.sock"

def bridge_call(method, *args):
    msg = msgpack.packb([0, 1, method, list(args)])   # [REQUEST=0, msgid, method, params]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCK)
        s.sendall(msg)
        resp = s.recv(4096)
    return msgpack.unpackb(resp)

# Example
bridge_call("set_haptic_grid", *([0]*21))
```

Install msgpack: `pip3 install --break-system-packages msgpack`

---

## STM32 Side (Arduino Sketch)

### Required library

Install in Arduino IDE: **Arduino_RouterBridge** (by Arduino)

Find it under: **Sketch → Include Library → Manage Libraries** → search "RouterBridge"

### Sketch template for receiving haptic data

```cpp
// TactileSight — STM32 haptic receiver via Arduino Bridge
// Receives 21-cell grid from QRB2210 Python via Bridge RPC.
// Upload to Arduino UNO Q board in Arduino IDE.

#include "Arduino_RouterBridge.h"

#define N_CELLS 21

// ── Pin mapping — edit to match your motor wiring ───────────────────────────
// One PWM pin per cell. Use -1 for unused cells.
const int MOTOR_PINS[N_CELLS] = {
//  col: 0   1   2   3   4   5   6
        -1, -1, -1, -1, -1, -1, -1,  // row 0 (top)
        -1, -1, -1, -1, -1, -1, -1,  // row 1 (middle)
        -1, -1, -1, -1, -1, -1, -1,  // row 2 (bottom)
};
// ─────────────────────────────────────────────────────────────────────────────

void set_haptic_grid(void) {
    // Bridge passes arguments; retrieve each cell value
    for (int i = 0; i < N_CELLS; i++) {
        int val = Bridge.getInt(i);   // 0–255
        val = constrain(val, 0, 255);
        if (MOTOR_PINS[i] >= 0) {
            analogWrite(MOTOR_PINS[i], val);
        }
    }
}

void setup() {
    Bridge.begin();
    Monitor.begin();

    // Register the handler — name must match Bridge.call("set_haptic_grid", ...) exactly
    Bridge.provide_safe("set_haptic_grid", set_haptic_grid);

    for (int i = 0; i < N_CELLS; i++) {
        if (MOTOR_PINS[i] >= 0) {
            pinMode(MOTOR_PINS[i], OUTPUT);
            analogWrite(MOTOR_PINS[i], 0);
        }
    }

    Monitor.println("haptic receiver ready");
}

void loop() {
    // Nothing here — Bridge callbacks are event-driven
    Bridge.update();
}
```

### Key rules for sketches on UNO Q

- **Use `Monitor.println()` instead of `Serial.println()`** for debug output — the
  Monitor goes to the App Lab console, not the Bridge channel.
- **Never call `Serial1.begin()` or open Serial1** — it's reserved.
- **Keep Bridge handlers fast** (< 1ms ideally) — avoid `delay()` inside them.
- **Call `Bridge.update()`** in `loop()` to process incoming RPC frames.

---

## Uploading the Sketch

1. Open Arduino IDE
2. **Tools → Board → Arduino UNO Q**
3. **Tools → Port** → select the port that appears when the board is connected via USB
   (while in device mode — the port only appears when USB is in device mode)
4. Open `linux/tactile_receiver.ino` (or the Bridge sketch above)
5. Fill in `MOTOR_PINS[]` for your wiring
6. **Sketch → Upload**

> **Note:** To upload, USB must be in **device mode**. Switch to host mode after upload
> for camera operation.
>
> ```bash
> # Switch to device mode to upload
> sudo /usr/local/bin/usb-role device
> # (upload via Arduino IDE)
> # Switch back to host mode for depth sensing
> sudo /usr/local/bin/usb-role host
> ```

---

## Cell Grid Layout

The 21 cells map to the wearer's body view:

```
col:   0    1    2    3    4    5    6
row 0 [00] [01] [02] [03] [04] [05] [06]   top of body (upper chest)
row 1 [07] [08] [09] [10] [11] [12] [13]   middle
row 2 [14] [15] [16] [17] [18] [19] [20]   bottom

col 0 = wearer's LEFT side
cell index i = row * 7 + col

Value 0   = no obstacle (open/far) → motor off
Value 255 = obstacle very close (≤350mm) → full vibration
```

---

## Motor Wiring Notes

- Use **PWM-capable pins** on the STM32 header for `analogWrite()` to work
- Do **not** drive motors directly from STM32 GPIO pins — ERM/LRA motors draw too much
  current (>50mA per motor). Use a transistor or motor driver:
  - Single motors: NPN transistor (e.g. 2N2222, BC547)
  - Multi-channel: DRV2605, DRV8833, L293D

```
STM32 PWM pin → Base of NPN transistor (via 1kΩ resistor)
Motor (+) → 5V (external supply recommended for >3 motors)
Motor (-) → Collector of NPN
Emitter → GND
Flyback diode across motor terminals (cathode to +)
```

---

## Current Status and Known Limitations

| Item | Status |
|------|--------|
| Bridge socket (`/var/run/arduino-router.sock`) | **Live and confirmed** on board |
| Python `arduino.app_utils` library | Needs verification — install via App Lab SDK |
| `uart_sender.py` (raw serial approach) | **Not working** — all TTYs reserved or I/O error |
| `/dev/ttyHS1` direct access | **Blocked** — reserved by `arduino-router` daemon |
| Bridge.call() from Python | **Documented path**, not yet tested with haptic sketch |

The current `uart_sender.py` subprocess will always fail because no usable raw serial port
exists. Once the Bridge sketch is uploaded and `arduino.app_utils` is confirmed installed,
replace `uart_sender.py` with the Bridge-based sender shown above.

---

## Verifying the Bridge

```bash
# On the board, confirm the socket is live
ls -la /var/run/arduino-router.sock
# srw-rw-rw- 1 root root 0 ... /var/run/arduino-router.sock

# Confirm bridge library is importable
python3 -c "from arduino.app_utils import *; print('bridge library ok')"

# Quick function call test (sketch must be uploaded with "ping" handler)
python3 -c "
from arduino.app_utils import *
result = Bridge.call('ping')
print('pong:', result)
"
```

---

## Alternative: Tactile Receiver (Raw UART — not on ttyHS1)

If you find an exposed UART header on the board that maps to a different `/dev/ttyXXX`,
the raw 24-byte packet approach in `linux/tactile_receiver.ino` can be used. That sketch
parses the binary protocol:

```
Byte  0:    0x55            sync A
Byte  1:    0xAA            sync B
Bytes 2-22: cell[0..20]     uint8, one byte per cell
Byte 23:    XOR(bytes 2-22) checksum
```

Set `HAPTIC_TTY=/dev/ttyXXX` in the `haptic-demo.service` Environment line to point
`uart_sender.py` at that device. But as of this writing, no such exposed UART has been
found — ttyS0–ttyS3 all return I/O errors.
