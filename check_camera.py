#!/usr/bin/env python3
"""
Orbbec Astra Pro Plus — camera diagnostic (Windows & Linux)

Requires only:  pip install pyorbbecsdk2
No separate SDK download needed — the native library is bundled in the package.

Steps:
  1. USB    — camera physically detected
  2. Package — pyorbbecsdk2 importable
  3. Device  — SDK finds the camera
  4. Info    — device name / firmware
  5. Depth   — firmware streams valid frames

Usage:
    python check_camera.py          (or:  check_camera.bat  on Windows)
"""
from __future__ import annotations
import platform
import subprocess

_BOLD   = "\033[1m"
_GREEN  = "\033[32m"
_RED    = "\033[31m"
_YELLOW = "\033[33m"
_RESET  = "\033[0m"

def ok(msg: str)   -> str: return f"{_GREEN}✓{_RESET} {msg}"
def fail(msg: str) -> str: return f"{_RED}✗{_RESET} {msg}"
def warn(msg: str) -> str: return f"{_YELLOW}⚠{_RESET} {msg}"
def head(msg: str) -> str: return f"\n{_BOLD}{msg}{_RESET}"

_IS_WINDOWS   = platform.system() == "Windows"
_SEARCH_TERMS = {"orbbec", "astra", "2bc5"}


# ── Step 1: USB ────────────────────────────────────────────────────────────────

def check_usb() -> bool:
    print(head("Step 1 — USB device detection"))
    if _IS_WINDOWS:
        return _usb_windows()
    return _usb_linux()


def _usb_windows() -> bool:
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-PnpDevice -PresentOnly | "
                "Where-Object { $_.FriendlyName -match 'Orbbec|Astra|2bc5' } | "
                "Format-List FriendlyName,Status",
            ],
            capture_output=True, text=True, timeout=15,
        )
        out = result.stdout.strip()
        if out:
            print(ok("Orbbec device found:"))
            for line in out.splitlines():
                if line.strip():
                    print(f"    {line.strip()}")
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


def _usb_linux() -> bool:
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
        print(fail("No Orbbec device found. Plug in the camera and retry."))
        return False
    except FileNotFoundError:
        print(warn("lsusb not found.  Install it:  sudo apt install usbutils"))
        return False


# ── Step 2: Package ────────────────────────────────────────────────────────────

def check_package() -> bool:
    print(head("Step 2 — pyorbbecsdk2 package"))
    try:
        from pyorbbecsdk import Context, OBLogLevel  # type: ignore
        Context.set_logger_to_console(OBLogLevel.ERROR)  # suppress SDK noise
        print(ok("pyorbbecsdk2 imported successfully."))
        return True
    except ImportError:
        print(fail("pyorbbecsdk2 not installed."))
        print("    Run:  pip install pyorbbecsdk2")
        return False


# ── Step 3: Device discovery ───────────────────────────────────────────────────

def check_device_found() -> bool:
    print(head("Step 3 — Device discovery"))
    from pyorbbecsdk import Context, OBLogLevel  # type: ignore
    Context.set_logger_to_console(OBLogLevel.ERROR)
    ctx = Context()
    device_list = ctx.query_devices()
    count = device_list.get_count()
    if count == 0:
        print(fail("No Orbbec device found by SDK."))
        if not _IS_WINDOWS:
            print("    → Add yourself to the plugdev group:")
            print("          sudo usermod -aG plugdev $USER   (log out and back in)")
        else:
            print("    → Check Device Manager for driver errors.")
        return False
    print(ok(f"Found {count} device(s)."))
    return True


# ── Step 4: Device info ────────────────────────────────────────────────────────

def check_device_info() -> bool:
    print(head("Step 4 — Device info"))
    from pyorbbecsdk import Context, OBLogLevel  # type: ignore
    Context.set_logger_to_console(OBLogLevel.ERROR)
    ctx = Context()
    device = ctx.query_devices().get_device_by_index(0)
    info = device.get_device_info()
    print(ok(f"Name      : {info.get_name()}"))
    print(ok(f"Serial    : {info.get_serial_number()}"))
    print(ok(f"Firmware  : {info.get_firmware_version()}"))
    print(ok(f"USB PID   : 0x{info.get_pid():04X}"))
    return True


# ── Step 5: Depth stream ───────────────────────────────────────────────────────

def check_depth_stream() -> bool:
    print(head("Step 5 — Depth stream"))
    from pyorbbecsdk import Context, Pipeline, OBLogLevel  # type: ignore
    import numpy as np
    Context.set_logger_to_console(OBLogLevel.ERROR)
    ctx     = Context()
    device  = ctx.query_devices().get_device_by_index(0)
    pipeline = Pipeline(device)
    try:
        pipeline.start()
        frames = pipeline.wait_for_frames(3000)
        if frames is None:
            print(fail("No frames received within 3 s — point camera at a scene and retry."))
            return False
        depth_frame = frames.get_depth_frame()
        if depth_frame is None:
            print(fail("Depth frame is None — camera may not support depth on this mode."))
            return False
        w     = depth_frame.get_width()
        h     = depth_frame.get_height()
        scale = depth_frame.get_depth_scale()
        data  = np.frombuffer(depth_frame.get_data(), dtype=np.uint16).reshape(h, w)
        depth_mm = data.astype(np.float32) * scale
        valid = depth_mm[depth_mm > 0]
        pct   = 100 * len(valid) / depth_mm.size
        rng   = f"{int(valid.min())}–{int(valid.max())} mm" if len(valid) else "n/a"
        print(ok(f"Frame {w}×{h}  valid={pct:.1f}%  range={rng}"))
        if pct < 20:
            print(warn("Low valid pixels — point camera at something within 3 m."))
        return True
    except Exception as exc:
        print(fail(f"Depth stream error: {exc}"))
        return False
    finally:
        pipeline.stop()


# ── Summary ────────────────────────────────────────────────────────────────────

def summary(results: dict[str, bool | None]) -> None:
    print(head("Summary"))
    labels = {
        "usb":    "USB device detected",
        "pkg":    "pyorbbecsdk2 installed",
        "found":  "Device found by SDK",
        "info":   "Device info read",
        "depth":  "Depth stream readable",
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
        print("    run.bat --source orbbec --scene wall_approach" if _IS_WINDOWS
              else "    python3 main.py --source orbbec --sink sim")
    else:
        print(f"{_YELLOW}{_BOLD}Not ready.{_RESET}  Fix the ✗ items, then re-run:")
        print("    check_camera.bat" if _IS_WINDOWS else "    python3 check_camera.py")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"{_BOLD}Orbbec Astra Pro Plus — camera diagnostic{_RESET}")
    print(f"{platform.system()} {platform.machine()}")

    results: dict[str, bool | None] = {}
    results["usb"]   = check_usb()
    results["pkg"]   = check_package()
    results["found"] = check_device_found()  if results["pkg"]   else None
    results["info"]  = check_device_info()   if results["found"] else None
    results["depth"] = check_depth_stream()  if results["info"]  else None

    summary(results)


if __name__ == "__main__":
    main()
