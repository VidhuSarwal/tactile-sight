#!/usr/bin/env python3
"""
Orbbec Astra Pro Plus — camera diagnostic (Windows & Linux)

Steps:
  1. USB    — camera physically detected
  2. Runtime — OpenNI2 native library found
  3. Init   — Python binding loads the library
  4. Device — SDK opens the camera
  5. Depth  — firmware streams valid frames

Usage:
    python check_camera.py
    python check_camera.py --sdk-path "C:/Program Files/OpenNI2/Redist"
"""
from __future__ import annotations
import argparse
import os
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

_IS_WINDOWS = platform.system() == "Windows"

# OpenNI2 library name differs by OS
_LIB_NAME = "OpenNI2.dll" if _IS_WINDOWS else "libOpenNI2.so"

# Orbbec USB search terms (vendor ID 2bc5)
_SEARCH_TERMS = {"orbbec", "astra", "2bc5"}

# ── Library search paths ───────────────────────────────────────────────────────

def _candidate_paths() -> list[Path]:
    if _IS_WINDOWS:
        candidates = [
            # The Orbbec/OpenNI2 installer sets this env var — fastest path
            Path(os.environ.get("OPENNI2_REDIST64", "")),
            Path(os.environ.get("OPENNI2_REDIST",   "")),
            Path("C:/Program Files/OpenNI2/Redist"),
            Path("C:/Program Files (x86)/OpenNI2/Redist"),
            Path("C:/OpenNI2/Redist"),
            Path("C:/Program Files/Orbbec/OpenNI2/Redist"),
        ]
    else:
        candidates = [
            Path("/usr/lib"),
            Path("/usr/local/lib"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib/aarch64-linux-gnu"),
            Path("/opt/OpenNI2"),
            Path.home() / "OpenNI2",
            Path.home() / "Downloads" / "OpenNI2",
        ]
    # also check CWD / ./Redist for users who extracted the SDK next to the script
    candidates += [Path("."), Path("Redist")]
    return [p for p in candidates if str(p)]  # drop empty strings from missing env vars


# ── Step 1: USB ────────────────────────────────────────────────────────────────

def check_usb() -> bool:
    print(head("Step 1 — USB device detection"))
    if _IS_WINDOWS:
        return _check_usb_windows()
    return _check_usb_linux()


def _check_usb_windows() -> bool:
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-PnpDevice -PresentOnly | "
                "Where-Object { $_.FriendlyName -match 'Orbbec|Astra|2bc5' } | "
                "Format-List FriendlyName,Status,InstanceId",
            ],
            capture_output=True, text=True, timeout=15,
        )
        out = result.stdout.strip()
        if out:
            print(ok("Orbbec device found:"))
            for line in out.splitlines():
                print(f"    {line}")
            return True
        print(fail("No Orbbec device found."))
        print("    → Plug in the Astra Pro Plus and retry.")
        print("    → Check Device Manager for unknown USB devices.")
        return False
    except FileNotFoundError:
        print(warn("PowerShell not available — skipping USB check."))
        return False
    except subprocess.TimeoutExpired:
        print(warn("USB scan timed out."))
        return False


def _check_usb_linux() -> bool:
    try:
        result = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=10
        )
        hits = [l for l in result.stdout.splitlines()
                if any(t in l.lower() for t in _SEARCH_TERMS)]
        if hits:
            print(ok("Orbbec device on USB:"))
            for l in hits:
                print(f"    {l.strip()}")
            return True
        print(fail("No Orbbec device found.  Plug in the camera and retry."))
        return False
    except FileNotFoundError:
        print(warn("lsusb not found.  Run:  sudo apt install usbutils"))
        return False


# ── Step 2: OpenNI2 runtime ────────────────────────────────────────────────────

def check_openni2_runtime(sdk_path: Path | None) -> Path | None:
    print(head("Step 2 — OpenNI2 runtime"))

    if sdk_path is not None:
        if not sdk_path.exists():
            print(fail(f"--sdk-path not found: {sdk_path}"))
            sdk_path = None
        else:
            lib = sdk_path / _LIB_NAME
            if lib.exists():
                print(ok(f"Found {_LIB_NAME} at: {lib}"))
                return sdk_path
            print(fail(f"{_LIB_NAME} not in {sdk_path}"))
            sdk_path = None

    for base in _candidate_paths():
        lib = base / _LIB_NAME
        if lib.exists():
            print(ok(f"Found {_LIB_NAME} at: {lib}"))
            return base

    print(fail(f"{_LIB_NAME} not found."))
    _install_instructions()
    return None


def _install_instructions() -> None:
    if _IS_WINDOWS:
        print("""
    Install the Orbbec OpenNI2 SDK for Windows:

    1. Download:  https://github.com/orbbec/OpenNI2/releases
       File: OpenNI-Windows-x64-2.x.x.zip  (or the .exe installer)

    2. Run the installer (or extract the zip).
       The installer sets OPENNI2_REDIST64 automatically.

    3. Re-run:  python check_camera.py
""")
    else:
        print("""
    Install OpenNI2 on Ubuntu:

    Option A (apt):
        sudo apt update && sudo apt install libopenni2-0 openni2-utils

    Option B (Orbbec SDK):  https://github.com/orbbec/OpenNI2/releases
        python check_camera.py --sdk-path <extracted>/Redist

    Re-run:  python check_camera.py
""")


# ── Step 3: SDK init ───────────────────────────────────────────────────────────

def check_sdk_init(lib_dir: Path) -> bool:
    print(head("Step 3 — SDK initialisation"))
    try:
        from openni import openni2
    except ImportError:
        print(fail("openni package missing.  Run:  pip install openni"))
        return False
    try:
        openni2.initialize(str(lib_dir))
        print(ok("OpenNI2 initialised."))
        return True
    except Exception as exc:
        print(fail(f"openni2.initialize() failed: {exc}"))
        print(f"    → Path tried: {lib_dir}")
        if not _IS_WINDOWS:
            print("    → Try: sudo usermod -aG plugdev $USER  (then log out/in)")
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
        if _IS_WINDOWS:
            print("    → Run Device Manager; check for driver errors on the Orbbec entry.")
        else:
            print("    → Try running as root once to rule out permissions.")
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
        pct    = 100 * len(valid) / data.size
        rng    = f"{int(valid.min())}–{int(valid.max())} mm" if len(valid) else "n/a"
        depth.stop()
        device.close()
        print(ok(f"Frame {w}×{h}  valid={pct:.1f}%  range={rng}"))
        if pct < 20:
            print(warn("Low valid pixels — point camera at something within 3 m."))
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
        print(f"{_GREEN}{_BOLD}Camera ready.{_RESET}")
        if _IS_WINDOWS:
            print("    run.bat --scene wall_approach")
        else:
            print("    python main.py --source orbbec --sink sim")
    else:
        print(f"{_YELLOW}{_BOLD}Camera not ready.{_RESET}  Fix the ✗ items, then re-run:")
        print("    python check_camera.py")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Orbbec Astra Pro Plus diagnostic")
    ap.add_argument(
        "--sdk-path", type=Path, default=None,
        help=f"Directory containing {_LIB_NAME}",
    )
    args = ap.parse_args()

    print(f"{_BOLD}Orbbec Astra Pro Plus — camera diagnostic{_RESET}")
    print(f"{platform.system()} {platform.machine()}")

    results: dict[str, bool | None] = {}
    results["usb"]     = check_usb()
    lib_dir            = check_openni2_runtime(args.sdk_path)
    results["runtime"] = lib_dir is not None
    results["init"]    = check_sdk_init(lib_dir)   if lib_dir           else None
    results["device"]  = check_device_open()        if results["init"]   else None
    results["depth"]   = check_depth_stream()       if results["device"] else None

    summary(results)


if __name__ == "__main__":
    main()
