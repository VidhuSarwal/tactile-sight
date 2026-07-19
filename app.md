# TactileSight — Full Bringup & Integration Guide

This guide covers everything needed to bring up the system: WiFi configuration, USB mode
switching, SSH access, all server endpoints, WebSocket integration, and haptic grid polling.

---

## Hardware

| Component | Detail |
|-----------|--------|
| Board | Arduino UNO Q (QRB2210 arm64 Linux + STM32U585 MCU) |
| Camera | Orbbec Astra Pro (depth 640×480, RGB 640×480) |
| Camera interface | USB-C in **host mode** |
| Server IP | `10.221.208.1` (assigned by DHCP on current WiFi) |
| HTTP port | `8081` |
| WebSocket port | `8083` |

---

## 1. Initial WiFi Setup

The UNO Q **boots in USB device mode** (exposing a USB gadget interface to the host PC).
This lets you set up WiFi before the camera is attached.

### Connect via USB (first time)

1. Plug USB-C from the board to your PC
2. The board appears as a USB serial/ADB device
3. Open a terminal and connect via ADB:
   ```bash
   adb shell
   ```
   or connect using the Arduino App Lab application on your PC.

### Set WiFi credentials

Once you have a shell on the board, use NetworkManager:

```bash
# List available networks
nmcli dev wifi list

# Connect to your network
nmcli dev wifi connect "YourSSID" password "YourPassword"

# Verify connection and get assigned IP
ip addr show wlan0
```

The IP shown on `wlan0` is your SSH address. Write it down — you'll use it to SSH in.

### Change WiFi later

```bash
# SSH in first (if already on WiFi), then:
nmcli connection show            # list saved connections
nmcli connection delete "OldSSID"
nmcli dev wifi connect "NewSSID" password "NewPassword"
```

### Make the board connect automatically

NetworkManager saves profiles automatically. On next boot, the board reconnects to the last
used network without any action needed.

---

## 2. SSH Access

Once the board is on WiFi:

```bash
ssh arduino@<board-ip>   # password: vidhu123
# Example: ssh arduino@10.221.208.1
```

The board's hostname is `DaddyDuino`. If your router supports mDNS:
```bash
ssh arduino@DaddyDuino.local
```

---

## 3. USB Mode: Device vs Host

The USB-C port on the UNO Q operates in one of two modes.

| Mode | When to use | Camera state |
|------|-------------|--------------|
| **device** | Initial setup, WiFi config, ADB shell, USB storage | Camera NOT powered — depth server idle |
| **host** | Normal depth sensing operation | Camera powered and streaming |

**The camera requires host mode.** Without it, `oniDeviceOpen()` fails and the server waits.

### Checking current mode

```bash
cat /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role
# prints: host   or   device
```

### Switching mode from SSH

```bash
# Switch to host mode (camera operation)
sudo /usr/local/bin/usb-role host

# Switch to device mode (PC connection / USB gadget)
sudo /usr/local/bin/usb-role device
```

When switching to host mode, the USB bus is power-cycled (brief ~3s delay before the
camera is detected). The depth server detects the camera automatically — no restart needed.

### Switching mode from the web UI

Open `http://<board-ip>:8081` in a browser. The **"Toggle host / device"** button handles
the power cycle automatically.

### Switching mode via HTTP

```bash
curl -X POST http://10.221.208.1:8081/toggle
# → {"usb_mode": "host"}   or   {"usb_mode": "device"}
```

### Recommended bringup sequence

```
1. Connect board to PC via USB-C (board boots in device mode)
2. ADB shell / App Lab → set WiFi credentials
3. Unplug USB-C from PC, plug in Orbbec Astra Pro depth camera
4. SSH into board: ssh arduino@<ip>
5. Switch to host mode: sudo /usr/local/bin/usb-role host
6. Start service if not already running: sudo systemctl start haptic-demo
7. Open http://<ip>:8081 — grid should animate as depth data flows
```

---

## 4. The Depth Server

The server (`linux/haptic_depth_server.py`) runs as a systemd service (`haptic-demo`).

### Service management

```bash
sudo systemctl start haptic-demo
sudo systemctl stop haptic-demo
sudo systemctl restart haptic-demo
sudo systemctl status haptic-demo
journalctl -u haptic-demo -f       # live logs
```

### Deploying updates from your dev machine

```bash
scp linux/haptic_depth_server.py arduino@10.221.208.1:~/
sshpass -p 'vidhu123' ssh arduino@10.221.208.1 "echo vidhu123 | sudo -S systemctl restart haptic-demo"
```

---

## 5. HTTP Endpoints

All endpoints are on `http://<board-ip>:8081`.

### `GET /`
Web dashboard: animated haptic grid, USB toggle, depth cam stream, capture button.
Open in any browser.

### `GET /grid`
Live haptic grid as JSON. Updated at ~30fps by the depth processing loop.

```bash
curl http://10.221.208.1:8081/grid
```

```json
{
  "grid":     [0, 0, 127, 255, 200, 0, 0, 0, 0, 50, ...],
  "raw_mm":   [0, 0, 950, 320, 580, 0, 0, 0, 0, 1400, ...],
  "status":   "streaming",
  "usb_mode": "host"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `grid[i]` | 0–255 | Haptic intensity: 0 = open/far, 255 = obstacle very close |
| `raw_mm[i]` | int mm | Raw median distance for that cell (0 = no return) |
| `status` | string | Camera state: `"streaming"`, `"waiting for camera"`, etc. |
| `usb_mode` | string | Current USB role: `"host"` or `"device"` |

Cell index: `i = row * 7 + col`

```
col:   0    1    2    3    4    5    6
row 0 [00] [01] [02] [03] [04] [05] [06]   top (wearer's body, upper chest)
row 1 [07] [08] [09] [10] [11] [12] [13]   middle
row 2 [14] [15] [16] [17] [18] [19] [20]   bottom

col 0 = wearer's LEFT side
```

### `GET /depth.mjpg`
Live colorized depth stream as MJPEG (requires `pillow` on board).

- Red = very close (<350mm)
- Yellow→Green = 350mm–2m
- Black = no depth return
- Frame rate: up to 10fps (camera runs at 30fps; MJPEG encodes at ≤10fps to save CPU)

Open in browser or `<img>` tag:
```html
<img src="http://10.221.208.1:8081/depth.mjpg">
```

### `POST /toggle`
Toggle USB-C between host and device modes. Returns `{"usb_mode": "host" | "device"}`.
When switching to host, automatically power-cycles the USB bus (~3s).

### `POST /capture`
One-shot capture: grabs the current depth frame + RGB frame and broadcasts a bundle to all
connected WebSocket clients.

```bash
curl -X POST http://10.221.208.1:8081/capture
# → {"ok": true}     on success
# → {"ok": false, "error": "camera not streaming"}   if no camera
```

---

## 6. WebSocket — One-shot Capture

The WebSocket server runs on port `8083`. Each `/capture` POST triggers one bundle message
sent to all connected clients.

### Connecting

```javascript
function connect() {
    const ws = new WebSocket('ws://10.221.208.1:8083');
    ws.onopen  = () => console.log('ws connected');
    ws.onclose = () => setTimeout(connect, 2000);  // auto-reconnect
    ws.onmessage = (e) => handleBundle(JSON.parse(e.data));
}
connect();
```

### Bundle format

```json
{
  "ts":        1752345678.123,
  "rgb_b64":   "<base64-encoded JPEG>",
  "depth_b64": "<base64-encoded PNG>"
}
```

| Field | Description |
|-------|-------------|
| `ts` | Unix timestamp (seconds) of the capture |
| `rgb_b64` | Base64 JPEG, 640×480, RGB colour from Orbbec Astra Pro |
| `depth_b64` | Base64 PNG, 640×480, 16-bit grayscale — pixel value = distance in mm |

### Reading depth values from the PNG

```javascript
function handleBundle(d) {
    // Show RGB preview
    document.getElementById('preview').src = 'data:image/jpeg;base64,' + d.rgb_b64;

    // Decode 16-bit depth PNG
    const img = new Image();
    img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = 640; canvas.height = 480;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(img, 0, 0);
        const pixels = ctx.getImageData(0, 0, 640, 480).data;
        // Each pixel: R = high byte, G = low byte of uint16 mm value
        const depthAtCenter = (pixels[(240*640+320)*4] << 8) | pixels[(240*640+320)*4 + 1];
        console.log('center depth:', depthAtCenter, 'mm');
    };
    img.src = 'data:image/png;base64,' + d.depth_b64;
}
```

### Python client example

```python
import asyncio, websockets, json, base64, struct, numpy as np

async def main():
    async with websockets.connect('ws://10.221.208.1:8083') as ws:
        async for msg in ws:
            d = json.loads(msg)
            rgb_jpg   = base64.b64decode(d['rgb_b64'])
            depth_raw = base64.b64decode(d['depth_b64'])
            print(f"ts={d['ts']:.1f}  rgb={len(rgb_jpg)//1024}KB  depth={len(depth_raw)//1024}KB")

asyncio.run(main())
```

### Triggering a capture from Python

```python
import requests
r = requests.post('http://10.221.208.1:8081/capture')
print(r.json())  # {"ok": true}
```

---

## 7. Polling the Haptic Grid

The grid updates at ~30fps independent of captures. Poll `/grid` at whatever rate you need:

```python
import requests, time

while True:
    r = requests.get('http://10.221.208.1:8081/grid').json()
    grid = r['grid']     # list of 21 ints, 0–255
    raw  = r['raw_mm']   # list of 21 ints, mm
    print(f"center cell: {grid[10]} ({raw[10]} mm)")
    time.sleep(0.1)      # poll at 10Hz
```

---

## 8. Shared Memory (IPC — advanced)

The haptic grid is also written to `/dev/shm/tactile/haptic_grid.bin` on the board (21 bytes,
one byte per cell, updated atomically via rename). Other processes on the board can read this
file directly without going through HTTP:

```bash
# On board:
python3 -c "
import time
while True:
    b = open('/dev/shm/tactile/haptic_grid.bin','rb').read()
    print([x for x in b])
    time.sleep(0.1)
"
```

---

## 9. Dependencies on the Board

```bash
# Already installed by setup.sh, but manual install if needed:
pip3 install --break-system-packages pillow websockets pypng pyserial

# OpenNI2 SDK: installed under ~/OpenNI_SDK/
# Orbbec driver: loaded via libOpenNI2.so in ~/OpenNI_SDK/.../NiViewer/
```

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `/grid` returns `"waiting for camera"` | USB in device mode or camera unplugged | Switch to host mode; check USB cable |
| Depth cam shows no image in browser | Camera not streaming yet | Wait ~5s after host-mode switch |
| Server unreachable after 20–60s | Old MJPEG memory bug | Update to latest `haptic_depth_server.py` |
| `oniDeviceOpen failed` in logs | USB in device mode | `sudo /usr/local/bin/usb-role host` |
| WiFi not connecting | Wrong SSID/password, or 5GHz-only AP | The WCBN3536A supports 2.4GHz and 5GHz |
| SSH refused | Board IP changed | Check `ip addr show wlan0` on board via ADB |
