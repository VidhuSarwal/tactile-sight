# TactileSight — STM32 Integration (Arduino Sketch)

The QRB2210 Linux side streams the 21-cell haptic grid to the internal STM32 over UART at
~30fps. The STM32 maps each cell value to a PWM output driving a haptic motor.

## Hardware

| Side | Device | Role |
|------|--------|------|
| Linux | QRB2210 (UNO Q) | Runs depth server, sends UART frames |
| MCU | Internal STM32 | Receives UART, drives PWM motors |
| UART | `/dev/ttyHS1` (Linux) ↔ `Serial` (STM32) | 115200 baud, 3.3V logic |

The Linux side opens `/dev/ttyHS1` automatically when the haptic-demo service starts.
Override with environment variable: `HAPTIC_TTY=/dev/ttyXXX` in the systemd service.

## Protocol

Each frame is **24 bytes**:

```
Byte  0:    0x55          sync A
Byte  1:    0xAA          sync B
Bytes 2-22: cell[0..20]   uint8, 0 = silent, 255 = maximum vibration
Byte 23:    checksum      XOR of bytes 2-22
```

Frames arrive at ~30fps. The receiver syncs on the `0x55 0xAA` header and validates the
XOR checksum before driving any motors. Corrupt or partial frames are silently discarded.

## Cell grid layout

```
col:   0    1    2    3    4    5    6
row 0 [00] [01] [02] [03] [04] [05] [06]   top of body
row 1 [07] [08] [09] [10] [11] [12] [13]   middle
row 2 [14] [15] [16] [17] [18] [19] [20]   bottom

col 0 = wearer's LEFT side
cell index i = row * 7 + col
```

Value 0 = no obstacle (silence), value 255 = obstacle very close (full vibration).

## Arduino sketch

The sketch is at `linux/tactile_receiver.ino`. Open it in Arduino IDE, fill in
`MOTOR_PINS[21]`, and upload to the STM32.

### Filling in MOTOR_PINS

Edit the array at the top of the sketch to map each cell index to a PWM pin:

```cpp
const int MOTOR_PINS[21] = {
//  col:   0   1   2   3   4   5   6
           3,  5,  6,  9, 10, 11, -1,   // row 0 (top): pins 3,5,6,9,10,11; col 6 unused
          -1, -1, -1, -1, -1, -1, -1,   // row 1 (middle): all unused in this example
          -1, -1, -1, -1, -1, -1, -1,   // row 2 (bottom): all unused
};
```

- Use any PWM-capable pin number for the STM32 variant you have
- Set to `-1` for cells with no motor attached (cell is silently skipped)

### Baud rate

Default is 115200. Both sides must match. To change it:
- Sketch: edit `#define BAUD 115200`
- Linux service: set `Environment=HAPTIC_BAUD=<rate>` in `linux/haptic-demo.service`

## Uploading the sketch

1. Connect to the UNO Q board via USB
2. In Arduino IDE: **Tools → Board** → select your STM32 variant
3. **Tools → Port** → select the STM32 COM port (different from the Linux UART)
4. Open `linux/tactile_receiver.ino`, fill in `MOTOR_PINS[]`
5. **Sketch → Upload**

The sketch starts receiving immediately after reset — no pairing or handshake needed.

## Verifying the UART link

### On the Linux side (board SSH)

```bash
# Check that uart_sender_loop started successfully
journalctl -u haptic-demo --no-pager | grep uart
# Expected: [srv] uart: opened /dev/ttyHS1 @ 115200

# Find the correct device if ttyHS1 doesn't work
pip3 install --break-system-packages pyserial
for d in /dev/ttyHS1 /dev/ttyS0 /dev/ttyS1 /dev/ttyS2; do
  echo -n "$d: "
  python3 -c "
import serial
s = serial.Serial('$d', 115200, timeout=0.5)
s.write(b'\x55')
r = s.read(1)
print(r.hex() if r else 'no echo')
" 2>&1
done

# Check that arduino user can open the port
groups arduino   # must include 'dialout'
# If not: sudo usermod -aG dialout arduino && reboot
```

### On the STM32 (Arduino Serial Monitor)

Add this to the sketch temporarily to print received values:

```cpp
// In apply_grid(), add:
Serial.print("grid: ");
for (int i = 0; i < N_CELLS; i++) {
    Serial.print(cells[i]);
    Serial.print(' ');
}
Serial.println();
```

Then open **Tools → Serial Monitor** at 115200 baud and watch the grid values change
as you move objects in front of the camera.

## Motor wiring notes

- `analogWrite(pin, 0)` = motor off, `analogWrite(pin, 255)` = full speed
- For ERM (eccentric rotating mass) vibration motors: wire via a transistor or motor driver
  (e.g. DRV8833, L293D) — do not drive directly from STM32 GPIO (exceeds current limit)
- Cell value 0 means no obstacle in that region → motor off
- Cell value 255 means obstacle ≤350mm away → motor at maximum

## Service configuration reference

`linux/haptic-demo.service` relevant lines:

```ini
Environment=HAPTIC_TTY=/dev/ttyHS1    # serial device for haptic UART
Environment=HAPTIC_BAUD=115200        # baud rate (must match sketch)
```

Change `HAPTIC_TTY` if the STM32 UART appears on a different device. After editing the
service file, redeploy:

```bash
scp linux/haptic-demo.service arduino@10.221.208.1:~/haptic-demo.service
ssh arduino@10.221.208.1 "echo vidhu123 | sudo -S cp ~/haptic-demo.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart haptic-demo"
```
