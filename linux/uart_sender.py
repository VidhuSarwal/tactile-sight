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
TTY       = os.environ.get("HAPTIC_TTY",  "/dev/ttyHS1")
BAUD      = int(os.environ.get("HAPTIC_BAUD", "115200"))

CANDIDATE_TTYS = [TTY, "/dev/ttyS0", "/dev/ttyS1", "/dev/ttyS2", "/dev/ttyS3"]

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
