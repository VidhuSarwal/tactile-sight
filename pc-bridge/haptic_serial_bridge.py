#!/usr/bin/env python3
"""Laptop bridge: depth server -> USB serial -> haptic band.

    camera board (:8081/grid)  --HTTP-->  THIS  --USB serial-->  Uno Q  -->  27 motors

Why this exists: the Arduino Router Bridge never registered `set_haptic_grid`
on our board, and a hackathon is the wrong place to keep debugging someone
else's RPC layer. Plain USB serial is already proven on this hardware. The
laptop does the fetching so the band's Uno Q only has to do one thing.

Counterpart sketch: linux/haptic_serial_receiver.ino

Wire format, 24 bytes:

    0xAA 0x55  <21 cell bytes, 0..255>  <XOR checksum of the 21>

Framed rather than newline-delimited because the payload is raw binary and 0x0A
is a legal intensity - a line protocol would cut frames in half at exactly the
value meaning "something is close".

Usage:
    python3 haptic_serial_bridge.py                        # autodetect port
    python3 haptic_serial_bridge.py --port /dev/ttyACM0
    python3 haptic_serial_bridge.py --host 10.89.1.1 --fps 25
    python3 haptic_serial_bridge.py --test                 # sweep, no camera
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit(
        "pyserial is not installed.\n"
        "  cd pc-bridge && ./run.sh          (sets up a venv and runs)\n"
        "or\n"
        "  pip install --user pyserial"
    )

HDR = bytes((0xAA, 0x55))
N_CELLS = 21
DEFAULT_HOST = "10.89.1.1"
DEFAULT_PORT_HTTP = 8081
BAUD = 115200

# Boards identify themselves differently depending on mode and vendor; match on
# any of these rather than pinning one VID:PID we would have to keep updating.
PORT_HINTS = ("arduino", "uno", "acm", "usb serial", "ch340", "stm")


def find_serial_port() -> str | None:
    """First port that looks like an Arduino. Explicit --port always wins."""
    ports = list(list_ports.comports())
    for p in ports:
        haystack = f"{p.description} {p.manufacturer} {p.device}".lower()
        if any(h in haystack for h in PORT_HINTS):
            return p.device
    # Nothing matched by name: if there is exactly one port, it is the one.
    return ports[0].device if len(ports) == 1 else None


def frame_for(cells: list[int]) -> bytes:
    """Header + 21 bytes + XOR checksum."""
    payload = bytes(max(0, min(255, int(c))) for c in cells[:N_CELLS])
    payload += bytes(N_CELLS - len(payload))          # pad short grids with 0
    checksum = 0
    for b in payload:
        checksum ^= b
    return HDR + payload + bytes((checksum,))


def fetch_grid(url: str, timeout: float = 2.0) -> list[int]:
    raw = urllib.request.urlopen(url, timeout=timeout).read()
    cells = json.loads(raw).get("grid") or []
    return [int(v) for v in cells[:N_CELLS]]


def test_pattern(step: int) -> list[int]:
    """A column sweep, so the band can be checked with no camera attached."""
    cells = [0] * N_CELLS
    col = step % 7
    for row in range(3):
        cells[row * 7 + col] = 200
    return cells



def open_serial(port: str | None, baud: int, forever: bool = True):
    """Open the port, waiting for it to appear rather than giving up.

    A band that stops working because someone nudged a USB cable, and stays
    stopped until a human notices, is worse than one that simply reconnects.
    The board also resets when the port opens, so every successful open pauses
    for the sketch to boot - without that, the first frames land in a
    bootloader that is not listening and the band sits dead until the next
    change of scene happens to redraw it.
    """
    warned = False
    while True:
        target = port or find_serial_port()
        if target:
            try:
                ser = serial.Serial(target, baud, timeout=0.05)
                time.sleep(2.0)              # let the sketch boot after reset
                ser.reset_input_buffer()
                print(f"serial connected: {target}")
                return ser
            except serial.SerialException as e:
                if not warned:
                    print(f"could not open {target}: {e}", file=sys.stderr)
                    if "Permission" in str(e):
                        print("  sudo usermod -aG dialout $USER   (then log out and in)",
                              file=sys.stderr)
                    warned = True
        elif not warned:
            print("waiting for the board to appear on USB...", file=sys.stderr)
            warned = True

        if not forever:
            return None
        time.sleep(2.0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"camera board address (default {DEFAULT_HOST}); it is DHCP, so it moves")
    ap.add_argument("--http-port", type=int, default=DEFAULT_PORT_HTTP)
    ap.add_argument("--port", help="serial port, e.g. /dev/ttyACM0. Autodetected if omitted")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--fps", type=float, default=25.0)
    ap.add_argument("--test", action="store_true",
                    help="send a moving column instead of camera data")
    ap.add_argument("--quiet", action="store_true", help="do not print the board's replies")
    args = ap.parse_args()

    grid_url = f"http://{args.host}:{args.http_port}/grid"
    print(f"serial : {args.port or 'autodetect'} @ {args.baud}")
    print(f"grid   : {'TEST PATTERN' if args.test else grid_url}")

    # Waits rather than exits: as a service this may well start before the
    # board is plugged in, and "not there yet" is not a failure.
    ser = open_serial(args.port, args.baud)
    if ser is None:
        return 1

    period = 1.0 / max(1.0, args.fps)
    step = 0
    sent = 0
    last_report = 0.0
    grid_failed = False

    try:
        while True:
            started = time.time()

            if args.test:
                cells = test_pattern(step // 8)
            else:
                try:
                    cells = fetch_grid(grid_url)
                    if grid_failed:
                        print(f"grid back: {grid_url}")
                        grid_failed = False
                except Exception as e:
                    if not grid_failed:
                        print(f"grid unreachable ({e}); sending zeros so the band goes quiet")
                        grid_failed = True
                    # Zeros, not nothing: the sketch's failsafe would cut the
                    # motors anyway, but saying so explicitly is instant.
                    cells = [0] * N_CELLS

            try:
                ser.write(frame_for(cells))
                ser.flush()
            except (serial.SerialException, OSError) as e:
                print(f"serial lost ({e}); reconnecting")
                try:
                    ser.close()
                except Exception:
                    pass
                ser = open_serial(args.port, args.baud)
                continue
            sent += 1
            step += 1

            # Drain the board's replies. It answers every frame, so this is the
            # proof the band is actually receiving.
            reply = b""
            try:
                while ser.in_waiting:
                    reply += ser.read(ser.in_waiting)
            except (serial.SerialException, OSError):
                reply = b""
            if reply and not args.quiet and time.time() - last_report > 1.0:
                line = reply.decode(errors="replace").strip().splitlines()[-1]
                on = sum(1 for c in cells if c >= 20)
                print(f"sent={sent} cells_on={on}/21 board: {line}")
                last_report = time.time()

            time.sleep(max(0.0, period - (time.time() - started)))

    except KeyboardInterrupt:
        print("\nstopping; band off")
        try:
            ser.write(frame_for([0] * N_CELLS))
            ser.flush()
        except Exception:
            pass
        ser.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
