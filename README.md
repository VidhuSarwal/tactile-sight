# TactileSight

Depth-to-haptic feedback system for the visually impaired. An Orbbec Astra Pro depth camera on the Arduino UNO Q (Qualcomm QRB2210 / arm64 Linux) converts proximity into haptic vibration patterns, sent over UART to an internal STM32 which drives motors on a vest.

---

## Hardware

| Component | Part |
|-----------|------|
| Main board | Arduino UNO Q (QRB2210, arm64, Debian 13) |
| Depth camera | Orbbec Astra Pro / Astra Pro Plus |
| Motor driver | Internal STM32 on the UNO Q |
| Hub | Powered USB hub (camera browns out without it) |

Board access: `arduino@10.221.208.1`, password `vidhu123`  
Web UI: `http://10.221.208.1:8081`

---

## Project files

### Linux (runs on the UNO Q)

| File | Purpose |
|------|---------|
| `linux/haptic_depth_server.py` | Main server — reads depth frames, computes 21-cell haptic grid, serves HTTP/MJPEG/WebSocket and triggers capture |
| `linux/rgb_worker.py` | Subprocess — captures RGB frame on demand via OpenCV |
| `linux/uart_sender.py` | Subprocess — sends 24-byte haptic grid packets to STM32 via UART |
| `linux/haptic-demo.service` | systemd unit — auto-starts the server, sets MALLOC tunables, restarts on crash |
| `linux/setup.sh` | One-time setup script — installs Python deps, udev rules, services |

### Arduino (runs on the internal STM32)

| File | Purpose |
|------|---------|
| `linux/tactile_receiver.ino` | Receives 24-byte haptic grid frames over UART, drives motor PWM pins |

---

## Quick start after SSH

```bash
# Check service status
systemctl status haptic-demo

# Watch live logs
journalctl -fu haptic-demo

# Open web UI in browser
# http://10.221.208.1:8081
```

The web UI provides:
- Live depth MJPEG stream
- Haptic grid visualization (21 cells, 3×7)
- USB host/device toggle (needed to connect the camera)
- Capture button (grabs RGB + depth PNG, broadcasts via WebSocket)

---

## HTTP API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web UI |
| `/grid` | GET | JSON `{grid, raw_mm, status, usb_mode}` |
| `/depth.mjpg` | GET | Live colorized depth MJPEG stream |
| `/toggle` | POST | Flip USB host ↔ device mode |
| `/capture` | POST | Trigger one-shot RGB+depth capture |
| `:8083` | WebSocket | Receives capture bundle `{ts, rgb_b64, depth_b64}` |

---

## Documentation

| File | Contents |
|------|----------|
| [app.md](app.md) | Full bringup guide — WiFi, USB host mode, SSH, endpoints, haptic grid IPC |
| [stm.md](stm.md) | Linux ↔ STM32 UART protocol, packet format, Arduino sketch guide |
| [hard-fact.md](hard-fact.md) | Camera specs, OpenNI2 SDK paths, known driver quirks on the UNO Q |
| [debug.md](debug.md) | Bug catalogue — root causes and fixes for all issues found during development |

---

## Deploy / update server

```bash
# From Mac:
scp linux/haptic_depth_server.py arduino@10.221.208.1:~/
scp linux/haptic-demo.service arduino@10.221.208.1:/tmp/

ssh arduino@10.221.208.1
echo 'vidhu123' | sudo -S cp /tmp/haptic-demo.service /etc/systemd/system/
echo 'vidhu123' | sudo -S systemctl daemon-reload
echo 'vidhu123' | sudo -S systemctl restart haptic-demo

# Verify MALLOC tunables loaded
cat /proc/$(pgrep -f haptic_depth_server.py | head -1)/environ | tr '\0' '\n' | grep MALLOC
```

---

## After a reboot

```bash
# 1. USB host mode should be automatic (usb-host-mode.service)
cat /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role
# expected: host

# 2. Camera detected on USB
lsusb | grep 2bc5   # expect two lines (depth 060f + RGB 050f)

# 3. Server running
systemctl status haptic-demo
```
