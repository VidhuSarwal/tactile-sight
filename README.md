# TactileSight Band — Increment 0: Depth-to-Haptic Pipeline

macOS development beta. Zero hardware required. Simulates the full depth-sensing → haptic-level pipeline that will run on the physical device.

## Requirements

Python 3.10+, `numpy`, `pyyaml`. No other dependencies.

```bash
pip install numpy pyyaml
```

## Running scenes

From the repo root:

```bash
# Wall approaching — all cells ramp from silent to max intensity
python main.py --source mock --scene wall_approach --sink sim

# Doorway on the left — left columns stay quiet, right columns active
python main.py --source mock --scene doorway_left --sink sim

# Person crossing — near blob marches from right to left across columns
python main.py --source mock --scene person_crossing --sink sim

# All clear — everything silent
python main.py --source mock --scene all_clear --sink sim

# Keyboard-step mode (press Enter to advance one frame at a time)
python main.py --source mock --scene wall_approach --sink sim --step
```

Press `Ctrl-C` to exit any scene cleanly.

## Running tests

```bash
pytest tests/ -v
```

## Adding a new scene

1. Open `src/tactile/depth_source.py`.
2. Write a generator function that yields `(8, 2)` `float32` NumPy arrays indefinitely (loop when the sequence ends). Use `np.nan` for invalid cells. Axis-0 = column (0 = wearer's left), axis-1 = row (0 = top).
3. Register it in the `_SCENES` dict at the bottom of the file:
   ```python
   _SCENES["my_scene"] = _my_scene
   ```
4. It becomes available immediately via `--scene my_scene`. Noise and occasional invalid cells are injected automatically by `MockSource`.

## What changes when the real camera adapter lands

Replace `MockSource` with an `OrbecSource(DepthSource)` class that calls `device.get_depth_frame()` on the Orbbec Astra Pro Plus SDK, averages depth pixels within each of the 8×2 spatial patches that map to the motor grid, and returns `np.nan` wherever the SDK reports invalid or out-of-range pixels. No other file changes are required — the `Encoder`, `SimDisplay`, and `main.py` pipeline are hardware-agnostic by design. The STM32 RPC sink similarly slots in as a `Sink` subclass that serializes the 16-byte frame over serial/USB rather than printing to the terminal.
