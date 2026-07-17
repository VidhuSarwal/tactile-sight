#!/usr/bin/env python3
"""
Orbbec Astra Pro Plus camera diagnostic — Ubuntu/Linux

Steps:
  1. USB — camera physically detected (lsusb)
  2. OpenNI2 runtime — libOpenNI2.so found
  3. SDK init — Python binding loads the library
  4. Device open — SDK enumerates the camera
  5. Depth stream — firmware streams valid frames

Usage:
    python3 check_camera.py
    python3 check_camera.py --sdk-path /path/to/OpenNI2-Redist
"""
from __future__ import annotations
import argparse
import platform
import subprocess
from pathlib import Path

_BOLD   = "\033[1m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_RESET  = "\033[0m"

def ok(msg: str)   -> str: return f"{_GREEN}✓{_RESET} {msg}"
def fail(msg: str) -> str: return f"{_RED}✗{_RESET} {msg}"
def warn(msg: str) -> str: return f"{_YELLOW}⚠{_RESET} {msg}"
def head(msg: str) -> str: return f"\n{_BOLD}{msg}{_RESET}"

_ORBBEC_VID   = "2bc5"
_SEARCH_TERMS = {"orbbec", "astra", "2bc5"}

_LIB_NAME = "libOpenNI2.so"

_CANDIDATE_PATHS: list[Path] = [
    Path("/usr/lib"),
    Path("/usr/local/lib"),
    Path("/usr/lib/x86_64-linux-gnu"),
    Path("/usr/lib/aarch64-linux-gnu"),
    Path("/opt/OpenNI2"),
    Path("/opt/orbbec"),
    Path.home() / "OpenNI2",
    Path.home() / "Downloads" / "OpenNI2",
    Path.home() / "orbbec" / "OpenNI2",
]


# ── Step 1: USB ────────────────────────────────────────────────────────────────

def check_usb() -> bool:
    print(head("Step 1 — USB device detection"))
    try:
        result = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=10
        )
        lines = result.stdout.splitlines()
        hits = [l for l in lines if any(t in l.lower() for t in _SEARCH_TERMS)]
        if hits:
            print(ok("Orbbec device on USB:"))
            for l in hits:
                print(f"    {l.strip()}")
            return True
        print(fail("No Orbbec device found.  Plug in the Astra Pro Plus and retry."))
        return False
    except FileNotFoundError:
        print(warn("lsusb not found.  Install it:  sudo apt install usbutils"))
        return False
    except subprocess.TimeoutExpired:
        print(warn("lsusb timed out."))
        return False


# ── Step 2: OpenNI2 runtime ────────────────────────────────────────────────────

def _find_lib() -> Path | None:
    for base in _CANDIDATE_PATHS:
        for candidate in [base / _LIB_NAME, base / "Redist" / _LIB_NAME]:
            if candidate.exists():
                return candidate.parent
    return None


def check_openni2_runtime(sdk_path: Path | None) -> Path | None:
    print(head("Step 2 — OpenNI2 runtime"))

    if sdk_path is not None and not sdk_path.exists():
        print(fail(f"--sdk-path not found: {sdk_path}"))
        sdk_path = None

    lib_dir = sdk_path or _find_lib()

    if lib_dir is None:
        print(fail(f"{_LIB_NAME} not found."))
        _install_instructions()
        return None

    print(ok(f"Found {_LIB_NAME} at: {lib_dir / _LIB_NAME}"))
    return lib_dir


def _install_instructions() -> None:
    arch = platform.machine()
    print(f"""
    Install OpenNI2 on Ubuntu ({arch}):

    Option A — apt (standard OpenNI2, usually enough):
        sudo apt update
        sudo apt install libopenni2-0 libopenni2-dev openni2-utils

    Option B — Orbbec SDK (required if Option A doesn't detect the camera):
        Download from https://github.com/orbbec/OrbbecSDK_ROS2 or
        the Orbbec developer site, extract, then:
            python3 check_camera.py --sdk-path <extracted>/Redist

    After installing, re-run:  python3 check_camera.py
""")


# ── Step 3: SDK init ───────────────────────────────────────────────────────────

def check_sdk_init(lib_dir: Path) -> bool:
    print(head("Step 3 — SDK initialisation"))
    try:
        from openni import openni2
    except ImportError:
        print(fail("openni Python package missing.  Run:  pip3 install openni"))
        return False

    try:
        openni2.initialize(str(lib_dir))
        print(ok("OpenNI2 initialised."))
        return True
    except Exception as exc:
        print(fail(f"openni2.initialize() failed: {exc}"))
        print(f"    → Path tried: {lib_dir}")
        print("    → If using apt's OpenNI2, try: --sdk-path /usr/lib/x86_64-linux-gnu")
        return False


# ── Step 4: Device open ────────────────────────────────────────────────────────

def check_device_open() -> bool:
    print(head("Step 4 — Open camera device"))
    from openni import openni2
    try:
        device = openni2.Device.open_any()
        info   = device.get_device_info()
        print(ok(f"Opened: {info.name!r}   URI: {info.uri!r}"))
        device.close()
        return True
    except openni2.OpenNIError as exc:
        print(fail(f"Cannot open device: {exc}"))
        print("    → Try: sudo usermod -aG plugdev $USER  (log out and back in)")
        print("    → Or run once as: sudo python3 check_camera.py")
        return False


# ── Step 5: Depth stream ───────────────────────────────────────────────────────

def check_depth_stream() -> bool:
    print(head("Step 5 — Depth stream"))
    from openni import openni2
    import numpy as np
    try:
        device = openni2.Device.open_any()
        depth  = device.create_depth_stream()
        depth.start()
        frame  = depth.read_frame()
        w, h   = frame.width, frame.height
        data   = np.frombuffer(frame.get_buffer_as_uint16(), dtype=np.uint16).reshape(h, w)
        valid  = data[data > 0]
        min_mm = int(valid.min()) if len(valid) else 0
        max_mm = int(valid.max()) if len(valid) else 0
        pct    = 100 * len(valid) / data.size
        depth.stop()
        device.close()
        print(ok(f"Frame {w}×{h}  valid={pct:.1f}%  range={min_mm}–{max_mm} mm"))
        if pct < 20:
            print(warn("Low valid pixel ratio — point camera at something within 3 m."))
        return True
    except Exception as exc:
        print(fail(f"Depth stream error: {exc}"))
        return False
    finally:
        try:
            from openni import openni2 as _o
            _o.unload()
        except Exception:
            pass


# ── Summary ────────────────────────────────────────────────────────────────────

def summary(results: dict[str, bool | None]) -> None:
    print(head("Summary"))
    labels = {
        "usb":     "USB device detected",
        "runtime": "OpenNI2 runtime found",
        "init":    "SDK initialised",
        "device":  "Camera opened",
        "depth":   "Depth stream readable",
    }
    all_pass = True
    for key, label in labels.items():
        v = results.get(key)
        if v is None:
            print(f"  {_YELLOW}–{_RESET} {label}  (skipped)")
        elif v:
            print(f"  {ok(label)}")
        else:
            print(f"  {fail(label)}")
            all_pass = False
    print()
    if all_pass:
        print(f"{_GREEN}{_BOLD}Camera ready.{_RESET}  Run:")
        print("    python3 main.py --source orbbec --sink sim")
    else:
        print(f"{_YELLOW}{_BOLD}Camera not ready.{_RESET}  Fix the ✗ items above, then:")
        print("    python3 check_camera.py")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Orbbec Astra Pro Plus diagnostic — Ubuntu")
    ap.add_argument(
        "--sdk-path", type=Path, default=None,
        help=f"Directory containing {_LIB_NAME} (overrides auto-search)",
    )
    args = ap.parse_args()

    print(f"{_BOLD}Orbbec Astra Pro Plus — camera diagnostic{_RESET}")
    print(f"Linux {platform.machine()}  kernel {platform.release()}")

    results: dict[str, bool | None] = {}

    results["usb"]     = check_usb()
    lib_dir            = check_openni2_runtime(args.sdk_path)
    results["runtime"] = lib_dir is not None
    results["init"]    = check_sdk_init(lib_dir)    if lib_dir  else None
    results["device"]  = check_device_open()         if results["init"]   else None
    results["depth"]   = check_depth_stream()        if results["device"] else None

    summary(results)


if __name__ == "__main__":
    main()
