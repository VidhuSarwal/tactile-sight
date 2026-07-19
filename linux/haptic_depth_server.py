#!/usr/bin/env python3
"""
TactileSight depth->haptic server  v6
  GET /           -> haptic grid web UI (USB toggle + depth cam + capture button)
  GET /grid       -> JSON {grid, raw_mm, status, usb_mode}
  GET /depth.mjpg -> colorized depth MJPEG stream (needs pillow)
  POST /toggle    -> flip USB-C host<->device (power-cycles camera when going to host)
  POST /capture   -> one-shot: grab rgb.jpg + depth.png + meta.json, push via WebSocket
  WS  :8083       -> JSON bundle {ts, rgb_b64, depth_b64} sent once per /capture call
  UART HAPTIC_TTY -> 24-byte binary frames at ~30fps → internal STM32 for haptic motors
  Port 8081 (HTTP) + 8083 (WebSocket)

Sensing: 21 cells (3 rows x 7 cols), each like an independent ultrasonic sensor.
  - DETECT_MM: alert range ceiling; beyond this = silence
  - Close obstacle = 255 (strong haptic), open/sky/no-return = 0 (silence)
  - 5-frame per-cell temporal median kills structured-light jitter

CRITICAL: Never import cv2 in this process — causes SIGSEGV with OpenNI2 ctypes.
          cv2 lives exclusively in rgb_worker.py (subprocess).
"""
import os, ctypes, threading, struct, time, json, subprocess, asyncio, base64, queue, gc
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    from PIL import Image
    import io as _io
    _MJPEG = True
except ImportError:
    _MJPEG = False

try:
    import websockets as _wslib
    _WS = True
except ImportError:
    _WS = False


NIDIR = os.path.expanduser(
    "~/OpenNI_SDK/OpenNI_2.3.0.86_202210111155_4c8f5aa4_beta6_a311d/tools/NiViewer")
os.chdir(NIDIR)
os.environ.setdefault("LD_LIBRARY_PATH", NIDIR)

ROLE_PATH = ("/sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb"
             "/usb_role/4e00000.usb-role-switch/role")
SHM_DIR          = "/dev/shm/tactile"
_HAPTIC_GRID_SHM = "/dev/shm/tactile/haptic_grid.bin"
COLS, ROWS = 7, 3
N_CELLS    = COLS * ROWS

DETECT_MM       = 2000
NEAR_FLOOR_MM   = 350
CELL_PERCENTILE = 20
MIN_VALID_FRAC  = 0.08
HIST_FRAMES     = 5

# ── haptic grid state ──────────────────────────────────────────────────────────
_grid_lock = threading.Lock()
_grid      = [0.0] * N_CELLS
_raw_mm    = [0]   * N_CELLS
_status    = ["starting..."]
_hist      = [[float(DETECT_MM)] * HIST_FRAMES for _ in range(N_CELLS)]
_hist_i    = 0

# ── frame pipeline: raw frames flow camera_loop → _raw_frame_q → frame_processor
# maxsize=2: camera_loop drops frames if processor is behind; OpenNI2 queue stays drained
_raw_frame_q = queue.Queue(maxsize=2)

# ── MJPEG stream ───────────────────────────────────────────────────────────────
# frame_processor shares raw bytes here; _stream_mjpeg() encodes JPEG in its own thread
_jpg_lock          = threading.Lock()
_depth_raw_mjpeg   = None   # latest raw depth bytes
_depth_frame_count = 0       # monotonic counter; MJPEG uses this to detect new frames
_mjpeg_cli_lock    = threading.Lock()
_mjpeg_clients     = 0

# ── snapshot / capture state ───────────────────────────────────────────────────
_snap_lock      = threading.Lock()
_snap_requested = False
_snap_ready_evt = threading.Event()
_snap_depth_raw = None        # bytes 640×480×2, set by frame_processor
_snap_ts        = 0.0

# ── capture queue — maxsize=1 so the latest press wins; processed by capture_worker
_capture_queue = queue.Queue(maxsize=1)

# ── WebSocket server state ─────────────────────────────────────────────────────
_ws_loop         = None       # asyncio event loop running in ws_server_loop thread
_ws_clients      = set()
_ws_clients_lock = threading.Lock()

log = lambda m: print(f"[srv] {m}", flush=True)

try:
    import io as _png_io, png as _png_lib
    def encode_depth_png(raw_bytes):
        # Build rows as memoryviews to avoid arr.tolist() (GIL-heavy pure Python)
        arr = np.frombuffer(raw_bytes, dtype=np.uint16).reshape(480, 640)
        buf = _png_io.BytesIO()
        _png_lib.Writer(width=640, height=480, bitdepth=16, greyscale=True).write(
            buf, (arr[r] for r in range(arr.shape[0])))
        return buf.getvalue()
    _HAVE_PNG = True
except ImportError:
    _HAVE_PNG = False
    def encode_depth_png(_):
        return b''


def get_usb_mode():
    try:
        return open(ROLE_PATH).read().strip()
    except Exception:
        return "unknown"


def colorize_depth(d_u16):
    valid = (d_u16 >= NEAR_FLOOR_MM) & (d_u16 < DETECT_MM)
    blind = d_u16 < NEAR_FLOOR_MM
    norm  = np.where(valid,
        np.clip((d_u16.astype(np.float32) - NEAR_FLOOR_MM) / (DETECT_MM - NEAR_FLOOR_MM), 0, 1),
        0.0)
    r = np.where(valid, np.clip((1.0 - norm) * 255, 0, 255), 0).astype(np.uint8)
    g = np.where(valid, np.clip(norm * 220, 0, 255), 0).astype(np.uint8)
    b = np.zeros(d_u16.shape, dtype=np.uint8)
    r[blind] = 70; g[blind] = 70; b[blind] = 70
    return np.stack([r, g, b], axis=2)


def process_frame(frame_bytes, w, h):
    global _hist_i, _depth_raw_mjpeg, _depth_frame_count
    d = np.frombuffer(frame_bytes, dtype=np.uint16).reshape(h, w)
    ch, cw = h // ROWS, w // COLS

    for idx in range(N_CELLS):
        r, c  = divmod(idx, COLS)
        cell  = d[r*ch:(r+1)*ch, c*cw:(c+1)*cw]
        valid = cell[cell >= NEAR_FLOOR_MM]
        min_px = max(5, int(cell.size * MIN_VALID_FRAC))
        if valid.size < min_px:
            _hist[idx][_hist_i % HIST_FRAMES] = float(DETECT_MM)
        else:
            _hist[idx][_hist_i % HIST_FRAMES] = float(np.percentile(valid, CELL_PERCENTILE))

    _hist_i += 1

    levels, raw_out = [], []
    for idx in range(N_CELLS):
        med = float(np.median(_hist[idx]))
        if med >= DETECT_MM:
            levels.append(0.0); raw_out.append(0)
        else:
            level = (DETECT_MM - med) / DETECT_MM * 255.0
            levels.append(min(255.0, max(0.0, level))); raw_out.append(int(med))

    # Share raw bytes for MJPEG — encoding happens in handler thread, not here
    with _mjpeg_cli_lock:
        has_clients = _mjpeg_clients > 0
    if _MJPEG and has_clients:
        with _jpg_lock:
            _depth_raw_mjpeg   = frame_bytes   # already a bytes copy from ctypes.string_at
            _depth_frame_count += 1

    return levels, raw_out


def camera_loop():
    """Reads raw depth frames from OpenNI2 as fast as possible and enqueues them.
    No numpy or PIL work happens here — that lives in frame_processor().
    Keeping this loop tight prevents OpenNI2's internal C++ frame queue from filling up,
    which was causing SIGSEGV from malloc failure in C++ code."""
    lib_path = os.path.join(NIDIR, "libOpenNI2.so")
    lib = ctypes.CDLL(lib_path)
    lib.oniInitialize.restype         = ctypes.c_int
    lib.oniDeviceOpen.restype         = ctypes.c_int
    lib.oniDeviceClose.restype        = ctypes.c_int
    lib.oniDeviceCreateStream.restype = ctypes.c_int
    lib.oniStreamStart.restype        = ctypes.c_int
    lib.oniStreamStop.restype         = None
    lib.oniStreamDestroy.restype      = None
    lib.oniStreamReadFrame.restype    = ctypes.c_int
    lib.oniWaitForAnyStream.restype   = ctypes.c_int
    lib.oniFrameRelease.restype       = None

    # Allow camera USB to settle after a process restart (prevents SIGSEGV during
    # oniInitialize when the USB device is still resetting from a prior crash)
    time.sleep(3)
    _status[0] = "initializing OpenNI2..."
    log(_status[0])
    if lib.oniInitialize(2) != 0:
        log("oniInitialize failed — camera_loop exiting")
        return

    while True:
        dev    = ctypes.c_void_p()
        stream = ctypes.c_void_p()
        try:
            _status[0] = "opening depth device..."
            if lib.oniDeviceOpen(None, ctypes.byref(dev)) != 0:
                raise RuntimeError("no camera — retrying...")

            if lib.oniDeviceCreateStream(dev, 3, ctypes.byref(stream)) != 0:
                raise RuntimeError("oniDeviceCreateStream(depth=3) failed")
            if lib.oniStreamStart(stream) != 0:
                raise RuntimeError("oniStreamStart failed")

            log("depth stream up — warming up...")
            for _ in range(5):
                frame = ctypes.c_void_p()
                lib.oniStreamReadFrame(stream, ctypes.byref(frame))
                if frame.value:
                    lib.oniFrameRelease(ctypes.byref(frame))

            _status[0] = "streaming"
            log(f"depth->haptic active | range 0-{DETECT_MM}mm | {HIST_FRAMES}-frame median"
                + (" | MJPEG ready" if _MJPEG else "")
                + (" | WS capture ready" if _WS else ""))

            streams_arr = (ctypes.c_void_p * 1)(stream.value)
            stream_idx  = ctypes.c_int(-1)
            timeout_streak = 0

            while True:
                rc_wait = lib.oniWaitForAnyStream(streams_arr, 1,
                                                  ctypes.byref(stream_idx), 500)
                if rc_wait != 0:
                    if rc_wait == 102:   # ONI_STATUS_TIME_OUT — transient, not fatal
                        timeout_streak += 1
                        if timeout_streak < 10:
                            continue
                        raise RuntimeError(f"stream timed out 10×  — USB disconnected?")
                    raise RuntimeError(f"stream lost (rc={rc_wait})")
                timeout_streak = 0

                frame = ctypes.c_void_p()
                rc = lib.oniStreamReadFrame(stream, ctypes.byref(frame))
                if rc != 0 or not frame.value:
                    continue

                # CRITICAL: string_at copies bytes — never use from_address with numpy loaded
                hdr       = ctypes.string_at(frame.value, 80)
                data_size = struct.unpack_from('<i', hdr, 0)[0]
                data_addr = struct.unpack_from('<Q', hdr, 8)[0]
                w         = struct.unpack_from('<i', hdr, 36)[0]
                h         = struct.unpack_from('<i', hdr, 40)[0]

                MAX_FRAME = 640 * 480 * 2  # 614400 bytes
                if (data_addr and 0 < data_size <= MAX_FRAME
                        and 0 < w <= 640 and 0 < h <= 480):
                    raw = ctypes.string_at(data_addr, data_size)
                    lib.oniFrameRelease(ctypes.byref(frame))
                    # Enqueue for processing; drop frame if processor is behind
                    # (prevents OpenNI2 C++ queue from filling → SIGSEGV)
                    try:
                        _raw_frame_q.put_nowait((raw, w, h))
                    except queue.Full:
                        pass
                    del raw  # release local ref immediately
                else:
                    lib.oniFrameRelease(ctypes.byref(frame))

        except Exception as e:
            _status[0] = f"waiting for camera ({e})"
            log(f"camera: {e}")
        finally:
            if stream.value:
                try: lib.oniStreamStop(stream)
                except Exception: pass
                try: lib.oniStreamDestroy(stream)
                except Exception: pass
            if dev.value:
                try: lib.oniDeviceClose(dev)
                except Exception: pass
            with _grid_lock:
                _grid[:]   = [0.0] * N_CELLS
                _raw_mm[:] = [0]   * N_CELLS
        log("retry in 5s...")
        time.sleep(5)


def frame_processor():
    """Dequeues raw frames from _raw_frame_q and does all numpy/haptic/shm work.
    Runs in a separate thread so camera_loop stays free to drain OpenNI2."""
    global _snap_requested, _snap_depth_raw, _snap_ts
    frame_count = 0
    while True:
        try:
            raw, w, h = _raw_frame_q.get(timeout=2)
        except queue.Empty:
            continue

        levels, raw_mm = process_frame(raw, w, h)

        with _grid_lock:
            _grid[:]   = levels
            _raw_mm[:] = raw_mm

        # Write grid to shm for uart_sender subprocess
        try:
            grid_bytes = bytes([max(0, min(255, int(v))) for v in levels])
            with open(_HAPTIC_GRID_SHM + '.tmp', 'wb') as f:
                f.write(grid_bytes)
            os.rename(_HAPTIC_GRID_SHM + '.tmp', _HAPTIC_GRID_SHM)
        except Exception:
            pass

        # Snapshot hook for capture
        with _snap_lock:
            if _snap_requested:
                _snap_depth_raw = raw
                _snap_ts        = time.time()
                _snap_requested = False
                _snap_ready_evt.set()

        del raw   # release frame bytes; GC can reclaim immediately

        frame_count += 1
        if frame_count % 500 == 0:
            gc.collect()


# ── WebSocket server ───────────────────────────────────────────────────────────

async def _ws_handler(websocket):
    with _ws_clients_lock:
        _ws_clients.add(websocket)
    log(f"ws client connected ({len(_ws_clients)} total)")
    try:
        await websocket.wait_closed()
    finally:
        with _ws_clients_lock:
            _ws_clients.discard(websocket)
        log(f"ws client disconnected ({len(_ws_clients)} remaining)")


async def _broadcast(bundle):
    with _ws_clients_lock:
        targets = set(_ws_clients)
    for ws in targets:
        try:
            await ws.send(bundle)
        except Exception:
            pass


async def _ws_main():
    global _ws_loop
    _ws_loop = asyncio.get_running_loop()
    log("ws capture server on :8083")
    async with _wslib.serve(_ws_handler, "0.0.0.0", 8083):
        await asyncio.Future()   # run forever


def uart_sender_watchdog():
    """Launch uart_sender.py as a subprocess; restart it if it crashes."""
    sender_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "uart_sender.py")
    if not os.path.exists(sender_path):
        log("uart: uart_sender.py not found — haptic UART disabled")
        return
    while True:
        try:
            proc = subprocess.Popen(["python3", sender_path],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            log(f"uart: sender started (pid {proc.pid})")
            for line in proc.stdout:
                log("uart-sub: " + line.decode(errors='replace').rstrip())
            proc.wait()
            log(f"uart: sender exited (rc={proc.returncode}) — restarting in 5s")
        except Exception as e:
            log(f"uart: watchdog error: {e}")
        time.sleep(5)


def ws_server_loop():
    if not _WS:
        log("websockets not installed — capture WS unavailable. Fix: pip3 install websockets")
        return
    asyncio.run(_ws_main())


def capture_worker():
    """Processes capture requests from _capture_queue one at a time.
    Runs in its own daemon thread so HTTP /capture returns immediately."""
    global _snap_requested
    while True:
        ts_trigger = _capture_queue.get()   # blocks until a request arrives

        if not _ws_loop:
            log("capture: ws server not ready — skipping")
            if _ws_loop:
                asyncio.run_coroutine_threadsafe(
                    _broadcast(json.dumps({"error": "ws server not ready"})), _ws_loop)
            continue

        if _status[0] != "streaming":
            log(f"capture: camera not ready ({_status[0]}) — skipping")
            asyncio.run_coroutine_threadsafe(
                _broadcast(json.dumps({"error": f"camera not ready: {_status[0]}"})),
                _ws_loop)
            continue

        # 1. Trigger rgb_worker
        try:
            with open(os.path.join(SHM_DIR, 'trigger.ts'), 'w') as f:
                f.write(str(ts_trigger))
        except Exception as e:
            log(f"capture: trigger write failed: {e}")

        # 2. Request depth frame from frame_processor
        _snap_ready_evt.clear()
        with _snap_lock:
            _snap_requested = True
        got = _snap_ready_evt.wait(timeout=3.0)

        if not got:
            log("capture: depth frame timeout — camera not streaming?")
            asyncio.run_coroutine_threadsafe(
                _broadcast(json.dumps({"error": "depth timeout"})), _ws_loop)
            continue

        with _snap_lock:
            d_raw = _snap_depth_raw
            ts    = _snap_ts

        # 3. Poll for rgb.jpg newer than the trigger timestamp (up to 1s)
        rgb_jpg  = b''
        rgb_path = os.path.join(SHM_DIR, 'rgb.jpg')
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                mtime = os.path.getmtime(rgb_path)
                if mtime >= ts_trigger:
                    rgb_jpg = open(rgb_path, 'rb').read()
                    break
            except Exception:
                pass
            time.sleep(0.05)
        if not rgb_jpg:
            log("capture: rgb.jpg not ready in 1s — sending depth only")

        # 4. Encode depth PNG and broadcast
        try:
            depth_png = encode_depth_png(d_raw)
        except Exception as e:
            log(f"capture: encode error: {e}")
            depth_png = b''

        bundle = json.dumps({
            "ts":        ts,
            "rgb_b64":   base64.b64encode(rgb_jpg).decode(),
            "depth_b64": base64.b64encode(depth_png).decode(),
        })
        asyncio.run_coroutine_threadsafe(_broadcast(bundle), _ws_loop)
        log(f"capture: sent {len(rgb_jpg)//1024}KB rgb + {len(depth_png)//1024}KB depth "
            f"to {len(_ws_clients)} client(s)")


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>TactileSight</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0a0a0a; color: #ccc; font-family: monospace;
         display: flex; flex-direction: column; align-items: center;
         min-height: 100vh; padding: 20px; }
  h1 { font-size: 1.2rem; letter-spacing: 4px; text-transform: uppercase;
       color: #888; margin: 14px 0 6px; }
  #status { font-size: 0.75rem; color: #555; margin-bottom: 16px; height: 1.2em; }
  #grid { display: grid; grid-template-columns: repeat(7, 52px);
          grid-template-rows: repeat(3, 52px); gap: 10px; }
  .dot { width: 52px; height: 52px; border-radius: 50%; background: #181818;
         transition: background 80ms, transform 80ms, box-shadow 80ms;
         display: flex; align-items: center; justify-content: center;
         font-size: 9px; color: rgba(255,255,255,0.45); user-select: none; }
  #usb-bar { margin-top: 24px; display: flex; align-items: center; gap: 10px;
             font-size: 0.8rem; }
  #mode-badge { padding: 3px 10px; border-radius: 4px; font-weight: bold;
                letter-spacing: 1px; }
  .host    { background: #0d2e0d; color: #3f3; border: 1px solid #2a2; }
  .device  { background: #2e0d0d; color: #f33; border: 1px solid #a22; }
  .unknown { background: #1e1e1e; color: #888; border: 1px solid #444; }
  #toggle-btn, #cap-btn { padding: 6px 14px; border: 1px solid #444;
                background: #1a1a1a; color: #aaa; border-radius: 4px;
                cursor: pointer; font-family: monospace; font-size: 0.8rem; }
  #toggle-btn:hover, #cap-btn:hover { background: #252525; }
  #toggle-btn:disabled, #cap-btn:disabled { opacity: 0.5; cursor: default; }
  #cap-btn.on { border-color: #3a3; color: #3f3; background: #0d1f0d; }
  #toggle-msg { font-size: 0.72rem; color: #666; margin-top: 6px;
                min-height: 1.2em; max-width: 440px; text-align: center; }
  .meta-link { margin-top: 12px; font-size: 0.72rem; color: #444;
               cursor: pointer; user-select: none; }
  .meta-link:hover { color: #777; }
  #depth-wrap { margin-top: 16px; display: none; }
  #depth-img  { max-width: 480px; width: 100%; border: 1px solid #2a2a2a;
                border-radius: 4px; display: block; }
  #depth-label { font-size: 0.65rem; color: #444; text-align: center;
                 margin-top: 4px; }
  #capture-bar { margin-top: 20px; display: flex; align-items: center;
                 gap: 10px; font-size: 0.8rem; }
  #snap-ts { font-size: 0.72rem; color: #555; }
  #ws-status { font-size: 0.65rem; color: #444; }
  #rgb-preview { margin-top: 12px; max-width: 320px; width: 100%;
                 border: 1px solid #2a2a2a; border-radius: 4px;
                 display: none; }
  #dl-bar { margin-top: 8px; display: none; gap: 12px; font-size: 0.72rem; }
  #dl-bar a { color: #4af; text-decoration: none; }
  #dl-bar a:hover { color: #7cf; }
</style>
</head>
<body>
<h1>TactileSight</h1>
<div id="status">connecting...</div>
<div id="grid"></div>

<div id="usb-bar">
  <span>USB-C mode:</span>
  <span id="mode-badge" class="unknown">?</span>
  <button id="toggle-btn" onclick="doToggle()">Toggle host / device</button>
</div>
<div id="toggle-msg"></div>

<div class="meta-link" id="debug-toggle" onclick="toggleDebug()">[ show mm ]</div>
<div class="meta-link" id="depth-toggle" onclick="toggleDepth()">[ show depth cam ]</div>

<div id="depth-wrap">
  <img id="depth-img" alt="depth stream" />
  <div id="depth-label">red = near (&lt;350mm) &nbsp;|&nbsp; yellow = mid &nbsp;|&nbsp; green = far (&gt;2m) &nbsp;|&nbsp; black = no return</div>
</div>

<div id="capture-bar">
  <button id="cap-btn" onclick="doCapture()">[ capture ]</button>
  <span id="snap-ts"></span>
  <span id="ws-status">ws: connecting...</span>
</div>
<img id="rgb-preview" alt="rgb snapshot">
<div id="dl-bar">
  <a id="dl-rgb"   download="rgb.jpg">↓ rgb.jpg</a>
  <a id="dl-depth" download="depth.png">↓ depth.png</a>
  <a id="dl-meta"  download="meta.json">↓ meta.json</a>
</div>

<script>
const statusEl   = document.getElementById('status');
const modeBadge  = document.getElementById('mode-badge');
const toggleBtn  = document.getElementById('toggle-btn');
const toggleMsg  = document.getElementById('toggle-msg');
const gridEl     = document.getElementById('grid');
const depthWrap  = document.getElementById('depth-wrap');
const depthImg   = document.getElementById('depth-img');
const depthToggle= document.getElementById('depth-toggle');
const capBtn     = document.getElementById('cap-btn');
const snapTs     = document.getElementById('snap-ts');
const wsStatus   = document.getElementById('ws-status');
const rgbPreview = document.getElementById('rgb-preview');
const dlBar      = document.getElementById('dl-bar');

let showDebug = false;
let depthOn   = false;
let capActive = false;
let lastRaw   = [];

// ── haptic grid ───────────────────────────────────────────────────────────────
const dots = [];
for (let i = 0; i < 21; i++) {
  const d = document.createElement('div');
  d.className = 'dot';
  gridEl.appendChild(d);
  dots.push(d);
}

function levelToStyle(v) {
  v = Math.max(0, Math.min(255, v));
  if (v < 8) return { bg: '#181818', shadow: 'none' };
  let r, g, b;
  if (v < 85) {
    const t = v / 85;
    r = 0; g = Math.round(30 + t * 180); b = 0;
  } else if (v < 170) {
    const t = (v - 85) / 85;
    r = Math.round(t * 220); g = Math.round(210 - t * 80); b = 0;
  } else {
    const t = (v - 170) / 85;
    r = 220; g = Math.round(130 - t * 130); b = 0;
  }
  const alpha = 0.5 + (v / 255) * 0.5;
  return {
    bg:     `rgb(${r},${g},${b})`,
    shadow: `0 0 ${Math.round(v/5)}px rgba(${r},${g},${b},${alpha.toFixed(2)})`
  };
}

function setMode(mode) {
  modeBadge.textContent = mode;
  modeBadge.className = ['host','device'].includes(mode) ? mode : 'unknown';
}

function toggleDebug() {
  showDebug = !showDebug;
  document.getElementById('debug-toggle').textContent = showDebug ? '[ hide mm ]' : '[ show mm ]';
  updateLabels();
}

function updateLabels() {
  for (let i = 0; i < 21; i++)
    dots[i].textContent = (showDebug && lastRaw[i]) ? lastRaw[i] : '';
}

function toggleDepth() {
  depthOn = !depthOn;
  if (depthOn) {
    depthWrap.style.display = 'block';
    depthImg.src = '/depth.mjpg?' + Date.now();
    depthToggle.textContent = '[ hide depth cam ]';
  } else {
    depthWrap.style.display = 'none';
    depthImg.src = '';
    depthToggle.textContent = '[ show depth cam ]';
  }
}

function poll() {
  fetch('/grid').then(r => r.json()).then(data => {
    const g = data.grid || [];
    lastRaw = data.raw_mm || [];
    for (let i = 0; i < 21; i++) {
      const s = levelToStyle(g[i] || 0);
      dots[i].style.background = s.bg;
      dots[i].style.boxShadow  = s.shadow;
      const sc = 1 + ((g[i] || 0) / 255) * 0.28;
      dots[i].style.transform  = `scale(${sc.toFixed(2)})`;
    }
    if (showDebug) updateLabels();
    statusEl.textContent = data.status || '';
    setMode(data.usb_mode || 'unknown');
  }).catch(() => {
    statusEl.textContent = 'server unreachable — retrying...';
  });
}

function doToggle() {
  const goingToHost = (modeBadge.textContent !== 'host');
  toggleBtn.disabled = true;
  toggleMsg.textContent = goingToHost ? 'power cycling camera (3s)...' : 'switching to device mode...';
  fetch('/toggle', { method: 'POST' }).then(r => r.json()).then(data => {
    const m = data.usb_mode;
    setMode(m);
    toggleMsg.textContent = m === 'device'
      ? 'Device mode — connect USB-C to PC. Click again to restore camera.'
      : 'Host mode — depth stream starting in ~5s.';
    setTimeout(() => { toggleMsg.textContent = ''; }, 8000);
  }).catch(() => {
    toggleMsg.textContent = 'toggle failed — check server logs';
  }).finally(() => { toggleBtn.disabled = false; });
}

// ── WebSocket (auto-reconnecting) ─────────────────────────────────────────────
let ws;
function connectWS() {
  ws = new WebSocket('ws://' + location.hostname + ':8083');
  ws.onopen  = () => { wsStatus.textContent = 'ws: connected'; };
  ws.onclose = () => {
    wsStatus.textContent = 'ws: reconnecting...';
    setTimeout(connectWS, 2000);
  };
  ws.onerror = () => {};   // onclose handles it

  ws.onmessage = function(e) {
    let d;
    try { d = JSON.parse(e.data); } catch { return; }
    if (d.error) {
      capBtn.disabled = false;
      capBtn.textContent = '[ capture ]';
      snapTs.textContent = 'failed: ' + d.error;
      return;
    }
    if (d.rgb_b64) {
      const rgbSrc = 'data:image/jpeg;base64,' + d.rgb_b64;
      rgbPreview.src = rgbSrc;
      rgbPreview.style.display = 'block';
      document.getElementById('dl-rgb').href = rgbSrc;
    }
    if (d.depth_b64) {
      document.getElementById('dl-depth').href =
        'data:image/png;base64,' + d.depth_b64;
    }
    if (d.ts) {
      document.getElementById('dl-meta').href =
        'data:application/json;base64,' + btoa(JSON.stringify({ts: d.ts}));
      snapTs.textContent = 'last: ' + new Date(d.ts * 1000).toLocaleTimeString();
    }
    dlBar.style.display = 'flex';
    capBtn.disabled = false;
    capBtn.textContent = '[ capture ]';
  };
}
connectWS();

function doCapture() {
  capBtn.disabled = true;
  capBtn.textContent = '[ capturing... ]';
  fetch('/capture', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      if (!d.ok) {
        capBtn.disabled = false;
        capBtn.textContent = '[ capture ]';
        snapTs.textContent = 'failed: ' + (d.error || 'unknown');
      }
      // on success: button re-enabled when WS message arrives with the bundle
    })
    .catch(() => {
      capBtn.disabled = false;
      capBtn.textContent = '[ capture ]';
      snapTs.textContent = 'request failed';
    });
}

setInterval(poll, 100);
poll();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _send(self, code, ctype, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == '/':
            self._send(200, "text/html; charset=utf-8", HTML)
        elif self.path == '/grid':
            with _grid_lock:
                g  = list(_grid)
                rm = list(_raw_mm)
            self._send(200, "application/json", json.dumps({
                "grid":           g,
                "raw_mm":         rm,
                "status":         _status[0],
                "usb_mode":       get_usb_mode(),
            }))
        elif self.path.startswith('/depth.mjpg'):
            self._stream_mjpeg()
        else:
            self._send(404, "text/plain", "not found")

    def _stream_mjpeg(self):
        global _mjpeg_clients
        if not _MJPEG:
            self._send(503, "text/plain",
                "pillow not installed — run: pip3 install pillow")
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        with _mjpeg_cli_lock:
            _mjpeg_clients += 1
        last_count = -1
        try:
            while True:
                with _jpg_lock:
                    raw   = _depth_raw_mjpeg
                    count = _depth_frame_count
                if raw and count != last_count:
                    last_count = count
                    # .copy() makes an independent numpy array so 'raw' bytes can be
                    # freed immediately — breaks the np.frombuffer base-chain reference
                    arr = np.frombuffer(raw, dtype=np.uint16).reshape(480, 640).copy()
                    raw = None   # release bytes reference NOW; frame_processor owns it
                    jpg = None
                    try:
                        rgb = colorize_depth(arr)
                        del arr
                        img = Image.fromarray(rgb, 'RGB')
                        del rgb
                        buf = _io.BytesIO()
                        img.save(buf, format='JPEG', quality=55)
                        del img
                        jpg = buf.getvalue()
                    except Exception as e:
                        log(f"mjpeg encode error: {e}")
                    if jpg:
                        try:
                            part = (
                                b"--frame\r\n"
                                b"Content-Type: image/jpeg\r\n"
                                b"Content-Length: " + str(len(jpg)).encode() + b"\r\n"
                                b"\r\n" + jpg + b"\r\n"
                            )
                            self.wfile.write(part)
                            self.wfile.flush()
                        except Exception:
                            break   # connection closed — exit handler, free memory
                time.sleep(0.1)   # encode at most 10fps, camera runs at 30fps
        except Exception:
            pass
        finally:
            with _mjpeg_cli_lock:
                _mjpeg_clients -= 1

    def do_POST(self):
        if self.path == '/toggle':
            current  = get_usb_mode()
            new_mode = "device" if current == "host" else "host"
            try:
                if new_mode == "host":
                    subprocess.run(["sudo", "/usr/local/bin/usb-role", "device"],
                        check=True, timeout=5, capture_output=True)
                    time.sleep(3)
                    subprocess.run(["sudo", "/usr/local/bin/usb-role", "host"],
                        check=True, timeout=5, capture_output=True)
                else:
                    subprocess.run(["sudo", "/usr/local/bin/usb-role", "device"],
                        check=True, timeout=5, capture_output=True)
                log(f"USB toggled -> {new_mode}")
            except Exception as e:
                log(f"toggle error: {e}")
            self._send(200, "application/json",
                json.dumps({"usb_mode": get_usb_mode()}))

        elif self.path == '/capture':
            try:
                _capture_queue.put_nowait(time.time())
                self._send(200, "application/json", json.dumps({"ok": True}))
            except queue.Full:
                self._send(200, "application/json",
                    json.dumps({"ok": False, "error": "capture in progress"}))

        else:
            self._send(404, "text/plain", "not found")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    if not _MJPEG:
        log("WARNING: pillow not found — /depth.mjpg unavailable. Fix: pip3 install pillow")
    if not _WS:
        log("WARNING: websockets not found — capture unavailable. Fix: pip3 install websockets")
    if not _HAVE_PNG:
        log("WARNING: png not found — depth.png will be empty. Fix: pip3 install pypng")

    os.makedirs(SHM_DIR, exist_ok=True)

    # rgb_worker subprocess — cv2 lives here, never imported in this process
    rgb_worker_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "rgb_worker.py")
    if os.path.exists(rgb_worker_path):
        subprocess.Popen(["python3", rgb_worker_path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log(f"rgb_worker started: {rgb_worker_path}")
    else:
        log(f"WARNING: rgb_worker.py not found — RGB capture disabled")

    threading.Thread(target=camera_loop,         daemon=True).start()
    threading.Thread(target=frame_processor,     daemon=True).start()
    threading.Thread(target=ws_server_loop,      daemon=True).start()
    threading.Thread(target=capture_worker,      daemon=True).start()
    threading.Thread(target=uart_sender_watchdog, daemon=True).start()

    log(f"TactileSight v6 | http://10.221.208.1:8081 | ws://10.221.208.1:8083"
        f" | detect 0-{DETECT_MM}mm | median/{HIST_FRAMES}f")
    srv = ThreadedHTTPServer(("0.0.0.0", 8081), Handler)
    srv.serve_forever()
