#!/usr/bin/env python3
"""
TactileSight bridge_sender — pushes the 21-cell haptic grid to the STM32U585
over the Arduino Router Bridge (MsgPack-RPC on a Unix socket).

This REPLACES uart_sender.py. See stm.md: the QRB2210 and the STM32 do not
share a raw serial port that user code may open. /dev/ttyHS1 is owned by the
arduino-router daemon and opening it desyncs the bridge (board reboot to
recover), and ttyS0-ttyS3 are not wired to the MCU. The only supported path is
the bridge RPC socket, which this script speaks directly so that we do not
depend on the `arduino.app_utils` package (not installed on this board).

IPC input:  /dev/shm/tactile/haptic_grid.bin   (21 bytes, written by frame_processor)
Transport:  /var/run/arduino-router.sock       (MsgPack-RPC)
Call:       set_haptic_grid(c0, c1, ... c20)   — must be registered by the sketch

Runs as a subprocess of haptic_depth_server.py, so a failure here cannot take
down the HTTP/WS server.

Wire format (MsgPack-RPC):
  request  [0, msgid, method, params]
  response [1, msgid, error, result]      error is nil on success,
                                          or [2, "method X not available"]
"""
import os, socket, sys, time

SOCK_PATH   = os.environ.get("BRIDGE_SOCK",   "/var/run/arduino-router.sock")
GRID_FILE   = os.environ.get("HAPTIC_GRID_SHM", "/dev/shm/tactile/haptic_grid.bin")
METHOD      = os.environ.get("HAPTIC_BRIDGE_METHOD", "set_haptic_grid")
FPS         = float(os.environ.get("HAPTIC_FPS", "30"))
N_CELLS     = 21

PERIOD      = 1.0 / FPS
KEEPALIVE_S = 1.0    # resend an unchanged grid at least this often
BACKOFF_S   = 2.0    # retry period while the sketch is missing / socket is down

log = lambda m: print(f"[bridge] {m}", flush=True)

try:
    import msgpack
except ImportError:
    log("msgpack not installed — exiting. Fix: pip3 install --break-system-packages msgpack")
    sys.exit(1)


class Bridge:
    """One persistent MsgPack-RPC connection to the arduino-router daemon."""

    def __init__(self, path):
        self.path = path
        self.sock = None
        self.unpacker = None
        self.msgid = 0

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(self.path)
        self.sock = s
        self.unpacker = msgpack.Unpacker(raw=False, strict_map_key=False)

    def close(self):
        if self.sock:
            try: self.sock.close()
            except Exception: pass
        self.sock = None
        self.unpacker = None

    def call(self, method, params):
        """Send one request and wait for its response. Raises on transport error."""
        self.msgid = (self.msgid + 1) & 0x7FFFFFFF
        self.sock.sendall(msgpack.packb([0, self.msgid, method, list(params)]))
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("bridge closed the connection")
            self.unpacker.feed(chunk)
            for msg in self.unpacker:
                # [type, msgid, error, result]
                if isinstance(msg, (list, tuple)) and len(msg) == 4 and msg[0] == 1:
                    return msg[2], msg[3]


def read_grid():
    """Return the current 21-byte grid, or None if unavailable/short."""
    try:
        g = open(GRID_FILE, 'rb').read()
        return g if len(g) == N_CELLS else None
    except FileNotFoundError:
        return None
    except Exception as e:
        log(f"grid read error: {e}")
        return None


def main():
    log(f"starting — sock={SOCK_PATH} method={METHOD} shm={GRID_FILE} fps={FPS:g}")

    bridge      = Bridge(SOCK_PATH)
    last_sent   = None
    last_tx     = 0.0
    warned_miss = False   # "method not available" already logged?
    warned_conn = False   # connection failure already logged?

    while True:
        # ── ensure connection ────────────────────────────────────────────────
        if bridge.sock is None:
            try:
                bridge.connect()
                log("connected to arduino-router")
                warned_conn = False
                last_sent = None          # force a resend after reconnect
            except Exception as e:
                if not warned_conn:
                    log(f"cannot connect ({e}) — retrying every {BACKOFF_S:g}s")
                    warned_conn = True
                time.sleep(BACKOFF_S)
                continue

        # ── read grid (all-zero until the camera streams) ────────────────────
        grid = read_grid()
        if grid is None:
            grid = bytes(N_CELLS)

        # ── send on change, or as a keepalive ────────────────────────────────
        now = time.time()
        if grid == last_sent and (now - last_tx) < KEEPALIVE_S:
            time.sleep(PERIOD)
            continue

        try:
            # ONE msgpack array argument, matching the sketch handler:
            #   void set_haptic_grid(MsgPack::arr_t<uint8_t> cells)
            # NOT 21 positional args — Arduino_RouterBridge deduces the handler
            # signature from the functor, so the arity must match exactly.
            err, _ = bridge.call(METHOD, [list(grid)])
        except Exception as e:
            log(f"transport error: {e} — reconnecting")
            bridge.close()
            time.sleep(1)
            continue

        if err is None:
            if warned_miss:
                log(f"'{METHOD}' is now available — haptics live")
                warned_miss = False
            last_sent, last_tx = grid, now
            time.sleep(PERIOD)
        else:
            # Sketch not uploaded yet, or handler name mismatch. Do not spam at 30fps.
            if not warned_miss:
                log(f"bridge rejected '{METHOD}': {err}")
                log("  -> upload the Bridge sketch to the STM32 "
                    "(linux/haptic_bridge_receiver.ino) so it registers this handler.")
                log(f"  -> retrying every {BACKOFF_S:g}s; server and LAN debug view are unaffected.")
                warned_miss = True
            time.sleep(BACKOFF_S)


if __name__ == '__main__':
    main()
