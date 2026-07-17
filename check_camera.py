#!/usr/bin/env python3
"""
TactileSight — Orbbec Astra Pro Plus camera diagnostic (macOS)

Run this before starting the real-hardware pipeline to confirm:
  1. USB — camera physically detected
  2. OpenNI2 runtime — native library found
  3. Device open — SDK can talk to the camera
  4. Depth stream — firmware streams valid frames

Usage:
    python3 check_camera.py
    python3 check_camera.py --sdk-path /path/to/OpenNI2-Redist
"""
from __future__ import annotations
import argparse
import platform
import subprocess
import sys
from pathlib import Path

# ── colour helpers ────────────────────────────────────────────────────────────
_BOLD  = "\033[1m"
_GREEN = "\033[32m"
_RED   = "\033[31m"
_YELLOW= "\033[33m"
_RESET = "\033[0m"

def ok(msg: str)   -> str: return f"{_GREEN}✓{_RESET} {msg}"
def fail(msg: str) -> str: return f"{_RED}✗{_RESET} {msg}"
def warn(msg: str) -> str: return f"{_YELLOW}⚠{_RESET} {msg}"
def head(msg: str) -> str: return f"\n{_BOLD}{msg}{_RESET}"

# ── Orbbec USB identifiers ────────────────────────────────────────────────────
# Astra Pro Plus vendor ID (Orbbec Technology Co.)
_ORBBEC_VID = "0x2bc5"
# Known product IDs for Astra Pro Plus (depth stream device)
_ASTRA_PRO_PLUS_PIDS = {"0x0501", "0x0502", "0x0503", "0x0536"}
_SEARCH_TERMS = {"orbbec", "astra", "2bc5"}

# ── Common locations where the Orbbec OpenNI2 Redist might be installed ───────
_CANDIDATE_PATHS: list[Path] = [
    Path.home() / "OpenNI2",
    Path.home() / "Downloads" / "OpenNI2",
    Path("/usr/local/lib"),
    Path("/opt/homebrew/lib"),
    Path("/opt/OpenNI2"),
    Path("/Library/OpenNI2"),
    # Orbbec SDK default install locations
    Path.home() / "Orbbec" / "OpenNI2",
    Path("/Applications/OpenNI2"),
]


# ── Step 1: USB detection ─────────────────────────────────────────────────────

def check_usb() -> bool:
    print(head("Step 1 — USB device detection"))
    try:
        result = subprocess.run(
            ["system_profiler", "SPUSBDataType"],
            capture_output=True, text=True, timeout=15,
        )
        text = result.stdout
        lower = text.lower()

        found = any(t in lower for t in _SEARCH_TERMS)
        if found:
            # Extract and print the relevant block
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if any(t in line.lower() for t in _SEARCH_TERMS):
                    block = "\n".join(
                        "    " + l for l in lines[max(0, i-1):min(len(lines), i+12)]
                    )
                    print(ok("Orbbec device detected on USB:"))
                    print(block)
                    return True
        print(fail("No Orbbec camera detected on USB."))
        print("    → Plug in the Astra Pro Plus via USB and run again.")
        print("    → If already plugged in, try a different cable or port.")
        return False

    except FileNotFoundError:
        print(warn("system_profiler not found — skipping USB check (not macOS?)."))
        return False
    except subprocess.TimeoutExpired:
        print(warn("USB scan timed out."))
        return False


# ── Step 2: OpenNI2 runtime ───────────────────────────────────────────────────

def find_openni2_lib() -> Path | None:
    """Return the directory containing libOpenNI2.dylib, or None."""
    lib_name = "libOpenNI2.dylib"
    for base in _CANDIDATE_PATHS:
        candidate = base / lib_name
        if candidate.exists():
            return base
        # Some distributions put the lib one level deeper (Redist/)
        candidate2 = base / "Redist" / lib_name
        if candidate2.exists():
            return base / "Redist"
    return None


def check_openni2_runtime(sdk_path: Path | None) -> Path | None:
    print(head("Step 2 — OpenNI2 runtime"))

    search_in = sdk_path if sdk_path else None

    if search_in and not search_in.exists():
        print(fail(f"--sdk-path does not exist: {search_in}"))
        search_in = None

    lib_dir = (search_in if search_in else None) or find_openni2_lib()

    if lib_dir is None:
        print(fail("libOpenNI2.dylib not found."))
        _print_install_instructions()
        return None

    lib_file = lib_dir / "libOpenNI2.dylib"
    print(ok(f"Found libOpenNI2.dylib at: {lib_file}"))
    return lib_dir


def _print_install_instructions() -> None:
    arch = platform.machine()
    print(f"""
    How to install the Orbbec OpenNI2 SDK (macOS {arch}):

    1. Download the Orbbec OpenNI2 SDK for macOS:
       https://www.orbbec.com/developers/openni-sdk/
       Choose: "OpenNI2 SDK for macOS" → macOS arm64 build (for Apple Silicon)

    2. Extract the archive. You'll get a folder like:
         OpenNI-MacOSX-<version>/

    3. Run this diagnostic again, pointing at the Redist folder:
         python3 check_camera.py --sdk-path ~/Downloads/OpenNI-MacOSX-.../Redist

       — OR — copy the libraries system-wide:
         sudo cp <redist>/libOpenNI2.dylib /usr/local/lib/
         sudo cp -r <redist>/OpenNI2 /usr/local/lib/

    Note: The standard OpenNI2 from Structure/StructureIO will NOT work
    with the Astra Pro Plus — you must use Orbbec's own build, which
    includes the Orbbec device driver plugin.
""")


# ── Step 3: SDK initialisation ────────────────────────────────────────────────

def check_sdk_init(lib_dir: Path) -> bool:
    print(head("Step 3 — SDK initialisation"))
    try:
        from openni import openni2
    except ImportError:
        print(fail("Python openni package not installed.  Run:  pip3 install openni"))
        return False

    try:
        openni2.initialize(str(lib_dir))
        print(ok("OpenNI2 initialized successfully."))
        return True
    except Exception as exc:
        print(fail(f"openni2.initialize() failed: {exc}"))
        print("    → Check that libOpenNI2.dylib is from Orbbec's SDK (not StructureIO).")
        print(f"    → Path tried: {lib_dir}")
        return False


# ── Step 4: Device open ───────────────────────────────────────────────────────

def check_device_open() -> bool:
    print(head("Step 4 — Open camera device"))
    from openni import openni2
    try:
        device = openni2.Device.open_any()
        info = device.get_device_info()
        print(ok(f"Opened device: {info.name!r}  URI: {info.uri!r}"))
        device.close()
        return True
    except openni2.OpenNIError as exc:
        print(fail(f"Cannot open device: {exc}"))
        print("    → Camera may not be connected or firmware needs a moment — retry.")
        return False


# ── Step 5: Depth stream ──────────────────────────────────────────────────────

def check_depth_stream() -> bool:
    print(head("Step 5 — Depth stream"))
    from openni import openni2
    try:
        device = openni2.Device.open_any()
        depth = device.create_depth_stream()
        depth.start()
        frame = depth.read_frame()
        w, h = frame.width, frame.height
        import numpy as np
        data = np.frombuffer(frame.get_buffer_as_uint16(), dtype=np.uint16).reshape(h, w)
        valid = data[data > 0]
        min_mm = int(valid.min()) if len(valid) else 0
        max_mm = int(valid.max()) if len(valid) else 0
        valid_pct = 100 * len(valid) / data.size
        depth.stop()
        device.close()
        print(ok(
            f"Depth frame: {w}×{h}  "
            f"valid={valid_pct:.1f}%  "
            f"range={min_mm}–{max_mm} mm"
        ))
        if valid_pct < 20:
            print(warn("Low valid pixel ratio — point camera at a scene within 3 m."))
        return True
    except Exception as exc:
        print(fail(f"Depth stream error: {exc}"))
        return False
    finally:
        try:
            from openni import openni2 as _oni2
            _oni2.unload()
        except Exception:
            pass


# ── Summary ───────────────────────────────────────────────────────────────────

def summary(results: dict[str, bool]) -> None:
    print(head("Summary"))
    labels = {
        "usb":    "USB device detected",
        "runtime":"OpenNI2 runtime found",
        "init":   "SDK initialised",
        "device": "Camera opened",
        "depth":  "Depth stream readable",
    }
    all_pass = True
    for key, label in labels.items():
        passed = results.get(key)
        if passed is None:
            print(f"  {_YELLOW}–{_RESET} {label}  (skipped)")
        elif passed:
            print(f"  {ok(label)}")
        else:
            print(f"  {fail(label)}")
            all_pass = False

    print()
    if all_pass:
        print(f"{_GREEN}{_BOLD}Camera is ready.{_RESET}  You can now run:")
        print("    python3 main.py --source orbbec --sink sim")
    else:
        print(f"{_YELLOW}{_BOLD}Camera not ready.{_RESET}  Address the ✗ items above, then re-run:")
        print("    python3 check_camera.py")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="TactileSight camera diagnostic")
    ap.add_argument(
        "--sdk-path", type=Path, default=None,
        help="Directory containing libOpenNI2.dylib (overrides auto-search)",
    )
    args = ap.parse_args()

    print(f"{_BOLD}TactileSight — Orbbec Astra Pro Plus diagnostic{_RESET}")
    print(f"macOS {platform.mac_ver()[0]}  {platform.machine()}")

    results: dict[str, bool | None] = {}

    results["usb"] = check_usb()

    lib_dir = check_openni2_runtime(args.sdk_path)
    results["runtime"] = lib_dir is not None

    if lib_dir is not None:
        results["init"] = check_sdk_init(lib_dir)
    else:
        results["init"] = None

    if results.get("init"):
        results["device"] = check_device_open()
    else:
        results["device"] = None

    if results.get("device"):
        results["depth"] = check_depth_stream()
    else:
        results["depth"] = None

    summary(results)


if __name__ == "__main__":
    main()
