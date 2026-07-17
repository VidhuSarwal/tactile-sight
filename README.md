# TactileSight Band — Increment 0

Depth-to-haptic pipeline. Runs fully without hardware (simulated camera, terminal display). Real Orbbec Astra Pro Plus slots in later with no code changes.

---

## Quick start — Windows (no camera needed)

**Requirements:** Python 3.10+ — https://www.python.org/downloads/
(the `python` command doesn't need to be in PATH — the bat files find it automatically)

```
1. Download the release ZIP from the Releases page and extract it anywhere.
2. Double-click  setup.bat  — installs numpy, pyyaml, openni via pip
3. Open a terminal in the folder and run a scene:
```

```bat
run.bat --scene wall_approach
run.bat --scene doorway_left
run.bat --scene person_crossing
run.bat --scene all_clear
```

Press `Ctrl-C` to stop. No camera, no drivers, nothing else needed.

---

## Scenes

| Scene | What you see |
|---|---|
| `wall_approach` | All cells ramp from silent to max as a wall closes in |
| `doorway_left` | Left columns stay quiet (open corridor), right columns activate |
| `person_crossing` | High-intensity blob marches right-to-left across columns |
| `all_clear` | All cells silent |

Step through frames manually (useful for debugging):
```bat
run.bat --scene wall_approach --step
```

---

## Adding a new scene

1. Open `src/tactile/depth_source.py`
2. Write a generator that yields `(8, 2)` float32 arrays indefinitely (loop at the end). Use `np.nan` for invalid cells. Axis 0 = column (0 = wearer's left), axis 1 = row (0 = top).
3. Register it in `_SCENES`:
   ```python
   _SCENES["my_scene"] = _my_scene
   ```
4. Run it: `run.bat --scene my_scene`

---

## Camera setup — Windows (Orbbec Astra Pro Plus)

**Step 1 — Install the Orbbec OpenNI2 SDK**

Download the Windows installer from:
https://github.com/orbbec/OpenNI2/releases

Run the `.exe`. It installs the driver and sets the `OPENNI2_REDIST64` environment variable automatically — no manual path configuration needed.

**Step 2 — Plug in the camera and verify**

```bat
check_camera.bat
```

Expected output when everything is working:
```
✓ USB device detected
✓ OpenNI2 runtime found
✓ SDK initialised
✓ Camera opened
✓ Depth stream readable
```

If Device 4 shows an error in Device Manager, re-run the Orbbec installer and select "Repair".

---

## Camera setup — Ubuntu (reference)

```bash
sudo apt install libopenni2-0 openni2-utils usbutils
pip3 install numpy pyyaml openni
python3 check_camera.py
```

If the camera is detected on USB but the device won't open:
```bash
sudo usermod -aG plugdev $USER   # log out and back in after this
```

---

## Running tests

```bat
test.bat
```

---

## What changes when the real camera is wired in

Replace `MockSource` with an `OrbecSource(DepthSource)` class that:
- Calls `openni2.Device.open_any()` and starts a depth stream
- Reads one frame per `get_grid()` call
- Averages depth pixels within each of the 8×2 spatial patches
- Returns `np.nan` where the SDK reports zero or out-of-range pixels

No other file changes needed — `Encoder`, `SimDisplay`, and `main.py` are hardware-agnostic.
