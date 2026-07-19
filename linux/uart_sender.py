#!/usr/bin/env python3
"""
TactileSight uart_sender — reads haptic grid from shm, sends 24-byte binary
frames to STM32 over serial at ~30fps. Runs as a subprocess of
haptic_depth_server.py so any crash here cannot kill the main server.

IPC input:  /dev/shm/tactile/haptic_grid.bin  (21 bytes, written by camera_loop)
Protocol:   0x55 0xAA [21 cells uint8] [XOR checksum]  → HAPTIC_TTY @ HAPTIC_BAUD
"""
import os, time, sys

GRID_FILE = "/dev/shm/tactile/haptic_grid.bin"
TTY       = os.environ.get("HAPTIC_TTY", "")
BAUD      = int(os.environ.get("HAPTIC_BAUD", "115200"))

# DO NOT add /dev/ttyHS1 here. It is owned by the arduino-router daemon; opening
# it desyncs the Linux<->STM32 bridge and needs a board reboot to recover (stm.md).
# ttyS0-ttyS3 are not wired to the MCU on the UNO Q and only return I/O errors.
# This sender is therefore OPT-IN: it does nothing unless HAPTIC_TTY names a real
# external UART. The supported MCU path is bridge_sender.py.
RESERVED_TTYS = {"/dev/ttyHS1"}

if not TTY:
    print("[uart] no HAPTIC_TTY set — nothing to do. The supported STM32 path is "
          "bridge_sender.py (Arduino Router Bridge). See stm.md.", flush=True)
    raise SystemExit(0)

if TTY in RESERVED_TTYS:
    print(f"[uart] refusing to open {TTY}: reserved by arduino-router; opening it "
          f"breaks the bridge until reboot (stm.md). Use bridge_sender.py.", flush=True)
    raise SystemExit(1)

CANDIDATE_TTYS = [TTY]

def log(msg):
    print(f"[uart] {msg}", flush=True)

try:
    import serial
except ImportError:
    log("pyserial not installed — exiting. Fix: pip3 install pyserial")
    sys.exit(1)


def open_port():
    """Try TTY candidates in order; return open Serial or None."""
    tried = []
    for dev in CANDIDATE_TTYS:
        try:
            port = serial.Serial(dev, BAUD, timeout=0.1)
            log(f"opened {dev} @ {BAUD}")
            return port
        except Exception as e:
            tried.append(f"{dev}: {e}")
    for msg in tried:
        log(f"  {msg}")
    return None


last_grid = bytes(21)
port      = None

log(f"starting — shm={GRID_FILE} tty={TTY} baud={BAUD}")

while True:
    # Open serial port (retry loop)
    if port is None:
        port = open_port()
        if port is None:
            log("no usable serial port — retry in 10s")
            time.sleep(10)
            continue

    # Read current grid (may not exist yet)
    try:
        grid = open(GRID_FILE, 'rb').read()
        if len(grid) == 21:
            last_grid = grid
    except FileNotFoundError:
        pass  # camera not streaming yet — send last known grid (all zeros initially)
    except Exception as e:
        log(f"grid read error: {e}")

    # Build and send packet
    chk = 0
    for b in last_grid: chk ^= b
    try:
        port.write(b'\x55\xaa' + last_grid + bytes([chk]))
    except Exception as e:
        log(f"send error: {e} — reopening port")
        try: port.close()
        except: pass
        port = None
        time.sleep(1)
        continue

    time.sleep(1.0 / 30)
