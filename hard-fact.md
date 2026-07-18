# Hard Facts — Arduino UNO Q + Orbbec Astra Pro Setup

Last updated: 2026-07-18

---

## Hardware

| Item | Detail |
|------|--------|
| Board | Arduino UNO Q — Qualcomm Dragonwing QRB2210, arm64, Debian 13 (trixie) |
| Camera | Orbbec Astra Pro / Astra Pro Plus |
| Depth sensor USB | VID:PID `2bc5:060f` — "ORBBEC Depth Sensor" |
| RGB sensor USB | VID:PID `2bc5:050f` — "USB 2.0 Camera" (UVC, /dev/video0) |
| Camera serial | ACR3B3300AZ |
| Camera firmware | FW 5.8.22 / HW 0 / Chip 6 / Sensor 0 / SYS 12 |
| SSH | `arduino@10.221.208.1`, password `vidhu123` (IP changed after reflash) |
| Desktop | XFCE4 + XRDP (connect via RDP to see GUI) |

---

## Critical Bug: USB-C Port Starts in Device Mode

**Symptom:** After ~16 seconds of boot, the QRB2210's single USB-C port switches from
host mode to USB gadget/device mode. All USB devices (including the camera hub) disconnect.
`lsusb` returns nothing. `/sys/bus/usb/devices/` is empty.

**Root cause:** The Qualcomm OTG state machine (triggered during ADSP/audio codec
initialization at boot second 16) tears down the xHCI host controller and re-enables the
DWC3 USB gadget. The `adbd` (Android Debug Bridge daemon) then starts in device mode.

**Fix (manual):**
```bash
echo host > /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role
```

**Fix (persistent):**
- `adbd.service` is **disabled** (was using device mode for ADB-over-USB).
- `/etc/systemd/system/usb-host-mode.service` — enabled, 25-second delayed oneshot that
  runs AFTER the Qualcomm ADSP OTG switch (~16s), so the switch to host sticks.

**Toggle host ↔ device mode (for MCU programming):**
- **Web UI**: `http://10.221.208.1:8081` → click "Toggle host / device"
- SSH stays up during toggle (it's on WiFi, not USB).
- The toggle also enables/disables `usb-host-mode.service` so the choice persists across reboots.

**USB role switch helper:** `/usr/local/bin/usb-role [host|device]` — can be called with `sudo -n`.

**sudoers rule:** `/etc/sudoers.d/usb-role-switch` — allows `arduino` user to run `usb-role`
and `systemctl enable/disable usb-host-mode.service` without password.

To verify USB host mode after reboot:
```bash
cat /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role
# expected: host
lsusb | grep 2bc5
# expected: two lines (060f + 050f)
```

---

## Correct SDK — OpenNI_SDK (NOT Orbbec SDK v2)

The Astra Pro is a **legacy OpenNI-protocol device**. The newer **Orbbec SDK v2**
(`orbbec/OrbbecSDK`) dropped support for it. Always use:

- Repo: `orbbec/OpenNI_SDK`
- Release used: `v2.3.0.86-beat6`
- Asset: `OpenNI_2.3.0.86_202210111155_4c8f5aa4_beta6_arm64.zip`  
  (contains a nested `.rar` → extract with `unrar`)
- Install location: `~/OpenNI_SDK/OpenNI_2.3.0.86_202210111155_4c8f5aa4_beta6_a311d/`
- Redist (library + drivers): `tools/NiViewer/`  
  (`libOpenNI2.so` + `OpenNI2/Drivers/liborbbec.so` + `libOniFile.so`)
- udev rules installed at: `/etc/udev/rules.d/558-orbbec-usb.rules`

---

## Depth Sensor Quirk: No Bulk OUT Endpoint

The `liborbbec.so` driver logs `Open endpoint 0x1 failed to USB endpoint not found on device!`
on every open. This is **benign** — the Astra Pro Plus (PID 060f) does not have a USB
bulk-out endpoint; it uses control transfers (EP 0) for commands. The driver handles this
gracefully and streams fine despite the warning.

---

## Verified Depth Output

```
[OK] DEPTH 640x480  nonzero=209926  min/max mm=541/9939
```

- Resolution: 640×480 @ 30 fps
- ~68% of pixels have valid depth in a typical scene
- Range: ~0.3m – ~10m (spec), 0.5m–10m observed

ONI sensor type values (important — wrong value opens IR stream instead of depth):
```
ONI_SENSOR_IR    = 1
ONI_SENSOR_COLOR = 2
ONI_SENSOR_DEPTH = 3   ← use this for depth
```

---

## RGB Camera (UVC)

Enumerates as standard V4L2 device at `/dev/video0` (metadata at `/dev/video1`).  
Driver: `uvcvideo`. Max resolution: **1920×1080 MJPG @ 30fps**.

Access directly:
```bash
v4l2-ctl -d /dev/video0 --all
ffplay /dev/video0               # needs display
```

---

## Running the Depth Stack

The haptic grid server runs automatically as a systemd service:
```bash
systemctl status haptic-demo.service
journalctl -fu haptic-demo.service
```

Web UI + live haptic grid: **http://10.221.208.1:8081**
Grid JSON API: `GET http://10.221.208.1:8081/grid`
USB toggle: `POST http://10.221.208.1:8081/toggle` (or the button on the web UI)

For manual headless depth check:
```bash
cd ~/OpenNI_SDK/OpenNI_2.3.0.86_202210111155_4c8f5aa4_beta6_a311d/tools/NiViewer
LD_LIBRARY_PATH=. ./SimpleRead   # prints middle-pixel depth-mm at 30fps
```

---

## Python Notes

- System Python is 3.13.
- The `openni` pip package (v2.3.0) **does not work on Python 3.13** — `CEnum.__new__`
  fails with `TypeError: abstract class` due to a breaking change in how Python 3.12+
  handles abstract class instantiation.
- Use the C binaries (`SimpleRead`, `DepthReaderPoll`) or the ctypes script instead.
- `python3-opencv` is installable via `apt install python3-opencv` for the RGB viewer.

---

## Device Power Notes

- **Do not power the camera from the UNO Q's USB-C port directly** — bus power browns
  out the depth stream. Always use a powered USB hub between the UNO Q and the camera.
- Max power draw for the Astra Pro is listed as 500mA on the depth sensor alone.

---

## After a Reboot — Checklist

```bash
# 1. Confirm host mode (should be automatic via usb-host-mode.service)
cat /sys/class/typec/port0/data_role    # shows "host [device]" (cosmetic quirk — ignore)
cat /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role  # must say: host

# 2. Confirm camera on USB
lsusb | grep 2bc5   # expect two lines

# 3. Quick depth check
cd ~/OpenNI_SDK/OpenNI_2.3.0.86_202210111155_4c8f5aa4_beta6_a311d/tools/NiViewer
LD_LIBRARY_PATH=. timeout 5 ./SimpleRead   # expect non-zero depth values within 2s
```
