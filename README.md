# TactileSight Band — Increment 0

Depth-to-haptic pipeline. Runs fully without hardware (simulated camera, terminal display). Real Orbbec Astra Pro Plus slots in later with no code changes.

---

## Quick start — Windows (no camera needed)

**Requirements:** Python 3.10+ — https://www.python.org/downloads/
(the `python` command doesn't need to be in PATH — the bat files find it automatically)

```
1. Download the release ZIP from the Releases page and extract it anywhere.
2. Double-click  setup.bat  — installs numpy, pyyaml, pyorbbecsdk2 via pip
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

No separate SDK download needed — `setup.bat` already installs everything.

Plug in the camera, then run:

```bat
check_camera.bat
```

Expected output when working:
```
✓ USB device detected
✓ pyorbbecsdk2 installed
✓ Device found by SDK
✓ Device info read
✓ Depth stream readable
```

If the device shows a yellow warning in Device Manager, right-click it → "Update driver" → "Search automatically".

---

## Camera setup — Linux (reference)

```bash
sudo apt install usbutils
pip3 install numpy pyyaml pyorbbecsdk2
sudo usermod -aG plugdev $USER   # log out and back in after this
python3 check_camera.py
```

---

## Running tests

```bat
test.bat
```

---

## What changes when the real camera is wired in

Replace `MockSource` with an `OrbecSource(DepthSource)` class that:
- Uses `pyorbbecsdk`: `Context` → `query_devices()` → `Pipeline` → `wait_for_frames()`
- Reads one depth frame per `get_grid()` call
- Averages depth pixels within each of the 8×2 spatial patches
- Returns `np.nan` where `depth_mm == 0` (camera reports invalid/out-of-range)

No other file changes needed — `Encoder`, `SimDisplay`, and `main.py` are hardware-agnostic.
