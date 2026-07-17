#!/usr/bin/env python3
"""
Orbbec Astra Pro Plus — camera checker
Standalone script. Run it; it installs what it needs automatically.

    Windows:  check_camera.bat         (or: py check_camera.py)
    Linux  :  python3 check_camera.py
"""
from __future__ import annotations
import glob
import os
import platform
import subprocess
import sys
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

_IS_WINDOWS   = platform.system() == "Windows"
_SEARCH_TERMS = {"orbbec", "astra", "2bc5"}


# ── Auto-install openni wrapper ────────────────────────────────────────────────

def _ensure_deps() -> bool:
    try:
        from openni import openni2  # noqa: F401
        return True
    except ImportError:
        pass
    print("Installing openni Python wrapper...")
    r = subprocess.run([sys.executable, "-m", "pip", "install", "openni", "numpy"])
    if r.returncode != 0:
        print(fail("pip install failed. Check your internet connection."))
        return False
    try:
        import importlib; importlib.invalidate_caches()
        from openni import openni2  # noqa: F401
        return True
    except ImportError:
        print(fail("Installed but import still fails — close and reopen this terminal."))
        return False


# ── Find OpenNI2 native library ────────────────────────────────────────────────

def _find_openni2_windows() -> Path | None:
    dll = "OpenNI2.dll"

    # 1. Env vars set by the OpenNI2 / Orbbec installer
    for var in ("OPENNI2_REDIST64", "OPENNI2_REDIST"):
        p = os.environ.get(var, "")
        if p and (Path(p) / dll).exists():
            return Path(p)

    # 2. Try loading via Windows DLL search path (works if installer put it in PATH)
    try:
        import ctypes
        lib = ctypes.WinDLL(dll)
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.kernel32.GetModuleFileNameW(lib._handle, buf, 512)
        return Path(buf.value).parent
    except Exception:
        pass

    # 3. Windows registry (OpenNI2 installer writes here)
    try:
        import winreg
        for subkey in (r"SOFTWARE\OpenNI2", r"SOFTWARE\WOW6432Node\OpenNI2"):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as k:
                    install_dir, _ = winreg.QueryValueEx(k, "Install_Dir")
                    for candidate in (Path(install_dir) / "Redist", Path(install_dir)):
                        if (candidate / dll).exists():
                            return candidate
            except OSError:
                pass
    except ImportError:
        pass

    # 4. Filesystem search under common install roots
    roots = [
        "C:/Program Files/OpenNI2",
        "C:/Program Files (x86)/OpenNI2",
        "C:/Program Files/Orbbec",
        "C:/Program Files (x86)/Orbbec",
        str(Path.home() / "OpenNI2"),
    ]
    for root in roots:
        for match in glob.glob(f"{root}/**/{dll}", recursive=True):
            return Path(match).parent

    return None


def _find_openni2_linux() -> Path | None:
    lib = "libOpenNI2.so"
    candidates = [
        Path("/usr/lib"),
        Path("/usr/local/lib"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/usr/lib/aarch64-linux-gnu"),
        Path("/opt/OpenNI2"),
        Path.home() / "OpenNI2",
    ]
    for base in candidates:
        if (base / lib).exists():
            return base
        if (base / "Redist" / lib).exists():
            return base / "Redist"
    return None


# ── Step 1: USB ────────────────────────────────────────────────────────────────

def check_usb() -> bool:
    print(head("Step 1 — USB device detection"))
    if _IS_WINDOWS:
        return _usb_windows()
    return _usb_linux()


def _usb_windows() -> bool:
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-PnpDevice -PresentOnly | "
             "Where-Object { $_.FriendlyName -match 'Orbbec|Astra|2bc5' } | "
             "Format-List FriendlyName,Status"],
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
        return False
    except FileNotFoundError:
        print(warn("PowerShell unavailable — skipping USB check."))
        return False
    except subprocess.TimeoutExpired:
        print(warn("USB scan timed out."))
        return False


def _usb_linux() -> bool:
    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=10)
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
        print(warn("lsusb not found.  Run:  sudo apt install usbutils"))
        return False


# ── Step 2: OpenNI2 runtime ────────────────────────────────────────────────────

def check_runtime(sdk_path: Path | None) -> Path | None:
    print(head("Step 2 — OpenNI2 runtime (from BiViewer install)"))

    if sdk_path is not None:
        lib_name = "OpenNI2.dll" if _IS_WINDOWS else "libOpenNI2.so"
        if not (sdk_path / lib_name).exists():
            print(fail(f"--sdk-path given but {lib_name} not found there: {sdk_path}"))
            sdk_path = None
        else:
            print(ok(f"Using --sdk-path: {sdk_path}"))
            return sdk_path

    lib_dir = _find_openni2_windows() if _IS_WINDOWS else _find_openni2_linux()

    if lib_dir:
        lib_name = "OpenNI2.dll" if _IS_WINDOWS else "libOpenNI2.so"
        print(ok(f"Found {lib_name} at: {lib_dir}"))
        return lib_dir

    print(fail("OpenNI2 runtime not found."))
    if _IS_WINDOWS:
        print("""
    The Orbbec BiViewer is installed but the OpenNI2 library was not found
    automatically. Find the folder that contains OpenNI2.dll inside the
    BiViewer install directory, then pass it explicitly:

        py check_camera.py --sdk-path "C:\\Program Files\\Orbbec\\...\\Redist"
""")
    else:
        print("    sudo apt install libopenni2-0  — then retry.")
    return None


# ── Step 3: SDK init ───────────────────────────────────────────────────────────

def check_init(lib_dir: Path) -> bool:
    print(head("Step 3 — SDK initialisation"))
    from openni import openni2
    try:
        openni2.initialize(str(lib_dir))
        print(ok("OpenNI2 initialised."))
        return True
    except Exception as exc:
        print(fail(f"openni2.initialize() failed: {exc}"))
        print(f"    → Path tried: {lib_dir}")
        return False


# ── Step 4: Device open ────────────────────────────────────────────────────────

def check_device() -> bool:
    print(head("Step 4 — Open camera device"))
    from openni import openni2
    try:
        device = openni2.Device.open_any()
        info   = device.get_device_info()
        print(ok(f"Opened : {info.name}"))
        print(ok(f"URI    : {info.uri}"))
        device.close()
        return True
    except openni2.OpenNIError as exc:
        print(fail(f"Cannot open device: {exc}"))
        if not _IS_WINDOWS:
            print("    → sudo usermod -aG plugdev $USER   (log out and back in)")
        return False


# ── Step 5: Depth stream ───────────────────────────────────────────────────────

def check_depth() -> bool:
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
            from openni import openni2 as _o; _o.unload()
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
        print(f"{_GREEN}{_BOLD}Camera is ready.{_RESET}")
    else:
        print(f"{_YELLOW}{_BOLD}Not ready.{_RESET}  Fix the ✗ items above and re-run.")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Orbbec Astra Pro Plus diagnostic")
    ap.add_argument("--sdk-path", type=Path, default=None,
                    help="Folder containing OpenNI2.dll (overrides auto-search)")
    args = ap.parse_args()

    print(f"{_BOLD}Orbbec Astra Pro Plus — camera checker{_RESET}")
    print(f"{platform.system()} {platform.machine()}")

    if not _ensure_deps():
        return

    results: dict[str, bool | None] = {}
    results["usb"]     = check_usb()
    lib_dir            = check_runtime(args.sdk_path)
    results["runtime"] = lib_dir is not None
    results["init"]    = check_init(lib_dir)    if lib_dir            else None
    results["device"]  = check_device()         if results["init"]    else None
    results["depth"]   = check_depth()          if results["device"]  else None

    summary(results)


if __name__ == "__main__":
    main()
