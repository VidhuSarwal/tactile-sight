# TactileSight

Depth-to-haptic feedback system for the visually impaired. An Orbbec Astra Pro depth camera on
the Arduino UNO Q (Qualcomm QRB2210 / arm64 Linux) reduces the scene to a 21-cell haptic grid
(7 columns x 3 rows), which is pushed to the on-board STM32U585 over the Arduino Router Bridge.
The MCU drives the band's vibration motors and mirrors the same obstacle picture onto the UNO Q's
LED matrix.

```
Orbbec Astra Pro ──USB(host)──► QRB2210 / Linux ──MsgPack-RPC──► STM32U585 ──I2C──► PCA9685 x2 ──► 27 motors
                                  depth -> 21-cell grid                                     └──► 13x8 LED matrix
                                  HTTP :8081 / WS :8083
```

---

## Hardware

| Component | Part |
|-----------|------|
| Main board | Arduino UNO Q (QRB2210 arm64 Debian 13 + STM32U585 Cortex-M33) |
| Depth camera | Orbbec Astra Pro / Astra Pro Plus (640x480 @ 30fps) |
| Motor driver | 2x PCA9685 16-channel PWM boards, I2C `0x40` and `0x60` |
| Motors | 27 vibration motors, 3 rows x 9 columns |
| Hub | Powered USB hub (camera browns out without it) |

### Board address

The board's IP is **DHCP-assigned and changes**. Older docs mention `10.221.208.1`; that is
stale. At the time of writing it is `10.89.1.1`. The server no longer hardcodes an address —
it detects and logs its real LAN IP at startup and binds `0.0.0.0`.

```bash
# From the board
ip -4 addr show wlan0

# Or read what the server logged
journalctl -u haptic-demo | grep "LAN debug UI"
```

SSH: `arduino@<board-ip>`, password `vidhu123`. Hostname is `DaddyDuino`.

---

## USB host vs device mode (read this first)

The UNO Q has a **single USB-C port in OTG mode**. It is either a host or a device, never both.

| Mode | What works | What does not |
|------|-----------|---------------|
| `host` | Depth camera enumerates and streams | ADB / App Lab over USB |
| `device` | ADB, App Lab, initial WiFi setup | Camera — the xHCI host controller is torn down, `lsusb` is empty |

```bash
# Check
cat /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role

# Switch
sudo /usr/local/bin/usb-role host      # camera
sudo /usr/local/bin/usb-role device    # ADB / App Lab
```

Also togglable from the web UI button or `POST /toggle`.

Persistence is handled by `usb-host-mode.service`, a 25-second delayed oneshot. The delay
matters: the Qualcomm ADSP/OTG state machine flips the port at ~16s into boot, so anything
written earlier is overwritten. To boot into device mode, **disable the unit** rather than
editing it.

Note: the STM32 sketch can be flashed **over WiFi** (the board appears as a network port), so
you do not need to drop to device mode just to upload a sketch.

---

## Quick start / bring-up

```bash
ssh arduino@<board-ip>            # password: vidhu123

# 1. Confirm host mode
cat /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role
# expected: host   (if not: sudo /usr/local/bin/usb-role host)

# 2. Confirm the camera is on the bus
lsusb | grep 2bc5                 # expect two lines: depth 060f + RGB 050f

# 3. Confirm the server
systemctl status haptic-demo
journalctl -fu haptic-demo

# 4. Open the dashboard
# http://<board-ip>:8081
```

The web UI provides a live depth MJPEG stream, the 21-cell haptic grid, a USB host/device
toggle, and a capture button.

First-time provisioning of a fresh board is `linux/setup.sh` (installs deps, OpenNI SDK, udev
rules, `usb-role` helper, `usb-host-mode.service`, and the haptic service).

---

## HTTP / WebSocket API

Server binds `0.0.0.0` — HTTP on `8081`, WebSocket on `8083`.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web dashboard |
| `/grid` | GET | JSON `{grid, raw_mm, status, usb_mode}` — 21 cells, 0-255 |
| `/depth.mjpg` | GET | Live colorized depth MJPEG |
| `/toggle` | POST | Flip USB host <-> device mode |
| `/capture` | POST | One-shot RGB+depth capture, pushed to WebSocket clients |
| `:8083` | WebSocket | Receives capture bundle `{ts, rgb_b64, depth_b64}` |

Grid indexing: `i = row * 7 + col`, col 0 = wearer's LEFT, row 0 = top. `0` = open/far,
`255` = obstacle very close. The grid is also mirrored to `/dev/shm/tactile/haptic_grid.bin`
(21 bytes) for on-board consumers. Full details in [app.md](app.md).

---

## Haptic path: Linux -> STM32

The raw-serial approach is dead and should not be revived:

- `/dev/ttyHS1` is **reserved** by the `arduino-router` daemon. Opening it desyncs the
  Linux<->STM32 bridge and requires a board reboot to recover.
- `ttyS0`-`ttyS3` are not wired to the MCU.

The supported path is the **Arduino Router Bridge**: MsgPack-RPC over the Unix socket
`/var/run/arduino-router.sock`.

| File | Role |
|------|------|
| `linux/bridge_sender.py` | Reads the grid from shm, calls `set_haptic_grid` over the bridge socket. Speaks MsgPack-RPC directly, so it has **no dependency on `arduino.app_utils`** (that package is not installed on the board). |
| `linux/haptic_bridge_receiver.ino` | STM32 sketch — registers `set_haptic_grid`, drives the PCA9685 motors and the LED matrix. |

The server picks its sender via the `HAPTIC_SENDER` env var (default `bridge_sender.py`), set
in `haptic-demo.service`. `linux/uart_sender.py` is retired: opt-in via `HAPTIC_TTY` only, and
it refuses `/dev/ttyHS1`.

Transport is verified working — persistent connection, 10 sequential RPC calls in 0.01s.

### The sketch

Target board FQBN `arduino:zephyr:unoq`.

```bash
arduino-cli compile -b arduino:zephyr:unoq linux/
arduino-cli upload  -b arduino:zephyr:unoq -p <board-ip> linux/    # network port, no USB switch needed
```

Key details:

- The handler takes **one MsgPack array argument** (`MsgPack::arr_t<uint8_t>`), not 21
  positional args. The old template in `stm.md` used `Bridge.getInt(i)`, which does not exist
  in Arduino_RouterBridge 0.4.2.
- **27 motors, 3 rows x 9 columns**, driven through two PCA9685 boards at `0x40` and `0x60`.
  Motor numbering is **column-major**: `motor = col * 3 + row`. Motors 0-13 are on `0x40`
  channels 0-13; motors 14-26 continue on `0x60` channels 0-12. PWM ~1kHz. The PCA9685 driver
  is implemented inline with `Wire` (no external library, for Zephyr-core compatibility).
- The depth grid is 7 columns but the harness is 9, so the two extra outermost columns repeat
  their neighbour via `COL_MAP = {0,0,1,2,3,4,5,6,6}`.
- The obstacle picture is mirrored onto the UNO Q's 13x8 LED matrix in 8-level grayscale
  (brighter = closer), so the board shows what the web UI shows.
- **Failsafe:** if no grid arrives for 1500 ms, all motors stop. A wearable that buzzes forever
  after the host dies is dangerous.

---

## Not yet verified

Two things are explicitly unproven and should not be assumed working:

1. **The sketch has never been run against real motor hardware.** No harness is available yet.
   The PCA9685 addressing, channel mapping, and column-major motor numbering are as-designed,
   not as-measured.
2. **Whether the depth sensor performs to spec in a normal scene is unconfirmed.** It has so far
   only been aimed at a ceiling with ceiling lights in frame, which is close to a worst case for
   structured-light depth.

---

## Recent fixes worth knowing about

Two bugs were fixed that had been blocking the camera entirely. Full write-ups in
[debug.md](debug.md).

- **BUG-010** — `setup.sh` generated a unit named `usb-host-mode.service` that actually wrote
  `device` to the role switch, with only a 2-second delay. Fresh setups therefore came up with
  no camera at all. Fixed to `sleep 25 && /usr/local/bin/usb-role host`.
- **BUG-009** — the real cause of the long-standing "SIGSEGV after ~2300 frames" crash,
  previously misdiagnosed (BUG-005) as an internal `libOpenNI2.so` bug. The actual cause was a
  malformed ctypes call: `oniFrameRelease(ctypes.byref(frame))` passed `OniFrame**` where the C
  API expects `OniFrame*`, corrupting the heap. Fixed at 3 call sites in `camera_reader.py` and
  3 in `haptic_depth_server.py`, plus an explicit `argtypes` declaration. Verified: 5,235 frames
  in 180s at ~29fps with zero crashes. The camera-subprocess isolation from BUG-005 is kept as
  defence in depth, but is no longer load-bearing; the periodic ~6-8s stream gap should be gone.

---

## Project files

### Linux (runs on the UNO Q) — active

| File | Purpose |
|------|---------|
| `linux/haptic_depth_server.py` | Main server — HTTP/MJPEG/WebSocket + haptic grid; spawns camera and haptic subprocesses |
| `linux/camera_reader.py` | Subprocess — reads OpenNI2 depth frames; isolated so a crash cannot kill the server |
| `linux/rgb_worker.py` | Subprocess — captures RGB frame on demand via OpenCV |
| `linux/bridge_sender.py` | Subprocess — pushes the grid to the STM32 over the Arduino Router Bridge |
| `linux/haptic-demo.service` | systemd unit — auto-start, MALLOC tunables, `HAPTIC_SENDER`, restart on crash |
| `linux/setup.sh` | One-time board provisioning |

### Arduino (runs on the STM32U585) — active

| File | Purpose |
|------|---------|
| `linux/haptic_bridge_receiver.ino` | Bridge receiver — PCA9685 motors + LED matrix mirror + failsafe |

### Stale / not used

| File | Notes |
|------|-------|
| `linux/uart_sender.py` | Raw-serial sender — retired; opt-in via `HAPTIC_TTY`, refuses `/dev/ttyHS1` |
| `linux/tactile_receiver.ino` | Raw 24-byte UART receiver — superseded by `haptic_bridge_receiver.ino` |
| `linux/matrix_display.ino` | Standalone LED matrix sketch |
| `linux/yolo_worker.py`, `linux/models/yolov8n.pt` | YOLO worker — not integrated |
| `linux/visualizer.html` | Standalone depth visualizer, not served |
| `main.py`, `check_camera.py`, `src/`, `tests/` | Windows simulation — not used in the Linux build |

---

## Deploy / update the server

```bash
scp linux/haptic_depth_server.py linux/camera_reader.py linux/bridge_sender.py arduino@<board-ip>:~/
scp linux/haptic-demo.service arduino@<board-ip>:/tmp/

ssh arduino@<board-ip>
echo 'vidhu123' | sudo -S cp /tmp/haptic-demo.service /etc/systemd/system/
echo 'vidhu123' | sudo -S systemctl daemon-reload      # always — see BUG-004
echo 'vidhu123' | sudo -S systemctl restart haptic-demo

# Verify MALLOC tunables actually loaded
cat /proc/$(pgrep -f haptic_depth_server.py | head -1)/environ | tr '\0' '\n' | grep MALLOC
```

---

## Troubleshooting

Start with [debug.md](debug.md) (bug catalogue with root causes) and
[hard-fact.md](hard-fact.md) (verified hardware facts, SDK paths, driver quirks).

Fast checks for the most common failure:

```bash
# "waiting for camera" / empty lsusb -> almost always USB mode
cat /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role
sudo /usr/local/bin/usb-role host

# Haptics silent -> is the sketch uploaded?
journalctl -u haptic-demo | grep bridge
# "bridge rejected 'set_haptic_grid'" means the sketch is not on the MCU yet.
# Once uploaded, bridge_sender.py picks it up on its own and logs "haptics live".
```

---

## Documentation

| File | Contents |
|------|----------|
| [app.md](app.md) | Bringup guide — WiFi, USB mode, SSH, all endpoints, haptic grid IPC |
| [stm.md](stm.md) | Linux <-> STM32 comms, bridge RPC, what is reserved, sketch rules |
| [hard-fact.md](hard-fact.md) | Camera specs, OpenNI2 SDK paths, known driver quirks |
| [debug.md](debug.md) | Bug catalogue — root causes and fixes |
