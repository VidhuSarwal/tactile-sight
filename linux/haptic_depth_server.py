#!/usr/bin/env python3
"""
TactileSight depth->haptic server  v4
  GET /          -> haptic grid web UI (USB toggle + depth cam toggle)
  GET /grid      -> JSON {grid, raw_mm, status, usb_mode}
  GET /depth.mjpg -> colorized depth MJPEG stream (needs pillow: pip3 install pillow)
  POST /toggle   -> flip USB-C host<->device
  Port 8081

Sensing: 21 cells (3 rows x 7 cols), each like an independent ultrasonic sensor.
  - DETECT_MM: alert range ceiling; beyond this = silence
  - Close obstacle = 255 (strong haptic), open/sky/no-return = 0 (silence)
  - 5-frame per-cell temporal median kills structured-light jitter
"""
import os, ctypes, threading, struct, time, json, subprocess
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

try:
    from PIL import Image
    import io as _io
    _MJPEG = True
except ImportError:
    _MJPEG = False

NIDIR = os.path.expanduser(
    "~/OpenNI_SDK/OpenNI_2.3.0.86_202210111155_4c8f5aa4_beta6_a311d/tools/NiViewer")
os.chdir(NIDIR)
os.environ.setdefault("LD_LIBRARY_PATH", NIDIR)

ROLE_PATH = ("/sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb"
             "/usb_role/4e00000.usb-role-switch/role")
COLS, ROWS = 7, 3
N_CELLS    = COLS * ROWS

DETECT_MM       = 2000   # objects beyond this are silence
NEAR_FLOOR_MM   = 350    # structured-light blind zone; discard below this
CELL_PERCENTILE = 20     # 20th pct of valid pixels per cell = nearest real point
MIN_VALID_FRAC  = 0.08   # at least 8% valid pixels needed per cell
HIST_FRAMES     = 5      # temporal median window

_grid_lock = threading.Lock()
_grid      = [0.0] * N_CELLS
_raw_mm    = [0]   * N_CELLS
_status    = ["starting..."]

_hist   = [[float(DETECT_MM)] * HIST_FRAMES for _ in range(N_CELLS)]
_hist_i = 0

_jpg_lock  = threading.Lock()
_depth_jpg = None   # latest colorized depth frame as JPEG bytes, or None

log = lambda m: print(f"[srv] {m}", flush=True)


def get_usb_mode():
    try:
        return open(ROLE_PATH).read().strip()
    except Exception:
        return "unknown"


def colorize_depth(d_u16):
    """Depth uint16 -> RGB: near=red, far=green, invalid/beyond=black, blind zone=grey."""
    valid = (d_u16 >= NEAR_FLOOR_MM) & (d_u16 < DETECT_MM)
    blind = d_u16 < NEAR_FLOOR_MM

    norm = np.where(valid,
        np.clip((d_u16.astype(np.float32) - NEAR_FLOOR_MM) / (DETECT_MM - NEAR_FLOOR_MM), 0, 1),
        0.0)

    r = np.where(valid, np.clip((1.0 - norm) * 255, 0, 255), 0).astype(np.uint8)
    g = np.where(valid, np.clip(norm * 220, 0, 255), 0).astype(np.uint8)
    b = np.zeros(d_u16.shape, dtype=np.uint8)

    r[blind] = 70; g[blind] = 70; b[blind] = 70

    return np.stack([r, g, b], axis=2)


def process_frame(frame_bytes, w, h):
    global _hist_i, _depth_jpg
    d = np.frombuffer(frame_bytes, dtype=np.uint16).reshape(h, w)
    ch, cw = h // ROWS, w // COLS

    for idx in range(N_CELLS):
        r, c = divmod(idx, COLS)
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

    if _MJPEG and _hist_i % 2 == 0:
        try:
            rgb = colorize_depth(d)
            img = Image.fromarray(rgb, 'RGB')
            buf = _io.BytesIO()
            img.save(buf, format='JPEG', quality=55)
            jpg = buf.getvalue()
            with _jpg_lock:
                _depth_jpg = jpg
        except Exception:
            pass

    return levels, raw_out


def camera_loop():
    lib_path = os.path.join(NIDIR, "libOpenNI2.so")
    while True:
        lib = None
        try:
            lib = ctypes.CDLL(lib_path)
            lib.oniInitialize.restype         = ctypes.c_int
            lib.oniDeviceOpen.restype         = ctypes.c_int
            lib.oniDeviceCreateStream.restype = ctypes.c_int
            lib.oniStreamStart.restype        = ctypes.c_int
            lib.oniStreamReadFrame.restype    = ctypes.c_int
            lib.oniFrameRelease.restype       = None
            lib.oniShutdown.restype           = None

            _status[0] = "initializing OpenNI2..."
            log(_status[0])
            if lib.oniInitialize(2) != 0:
                raise RuntimeError("oniInitialize failed")

            _status[0] = "opening depth device..."
            log(_status[0])
            dev = ctypes.c_void_p()
            if lib.oniDeviceOpen(None, ctypes.byref(dev)) != 0:
                raise RuntimeError("no camera — retrying...")

            stream = ctypes.c_void_p()
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
                + (" | MJPEG stream ready" if _MJPEG else " | (install pillow for MJPEG stream)"))

            while True:
                frame = ctypes.c_void_p()
                rc = lib.oniStreamReadFrame(stream, ctypes.byref(frame))
                if rc != 0 or not frame.value:
                    time.sleep(0.005)
                    continue

                # CRITICAL: string_at copies bytes — never use from_address with numpy loaded
                hdr       = ctypes.string_at(frame.value, 80)
                data_size = struct.unpack_from('<i', hdr, 0)[0]
                data_addr = struct.unpack_from('<Q', hdr, 8)[0]
                w         = struct.unpack_from('<i', hdr, 36)[0]
                h         = struct.unpack_from('<i', hdr, 40)[0]

                if data_addr and data_size > 0 and w > 0 and h > 0:
                    raw = ctypes.string_at(data_addr, data_size)
                    lib.oniFrameRelease(ctypes.byref(frame))
                    levels, raw_mm = process_frame(raw, w, h)
                    with _grid_lock:
                        _grid[:]   = levels
                        _raw_mm[:] = raw_mm
                else:
                    lib.oniFrameRelease(ctypes.byref(frame))

        except Exception as e:
            _status[0] = f"waiting for camera ({e})"
            log(f"camera: {e}")
            if lib:
                try: lib.oniShutdown()
                except Exception: pass
            with _grid_lock:
                _grid[:]   = [0.0] * N_CELLS
                _raw_mm[:] = [0]   * N_CELLS
            log("retry in 5s...")
            time.sleep(5)


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
  #toggle-btn { padding: 6px 14px; border: 1px solid #444; background: #1a1a1a;
                color: #aaa; border-radius: 4px; cursor: pointer;
                font-family: monospace; font-size: 0.8rem; }
  #toggle-btn:hover { background: #252525; }
  #toggle-btn:disabled { opacity: 0.5; cursor: default; }
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

<script>
const statusEl   = document.getElementById('status');
const modeBadge  = document.getElementById('mode-badge');
const toggleBtn  = document.getElementById('toggle-btn');
const toggleMsg  = document.getElementById('toggle-msg');
const gridEl     = document.getElementById('grid');
const depthWrap  = document.getElementById('depth-wrap');
const depthImg   = document.getElementById('depth-img');
const depthToggle= document.getElementById('depth-toggle');

let showDebug = false;
let depthOn   = false;
let lastRaw   = [];

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
    depthImg.src = '';   // closes MJPEG connection
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
  toggleBtn.disabled = true;
  toggleMsg.textContent = 'switching mode...';
  fetch('/toggle', { method: 'POST' }).then(r => r.json()).then(data => {
    const m = data.usb_mode;
    setMode(m);
    toggleMsg.textContent = m === 'device'
      ? 'Device mode — connect USB-C to PC to program MCU. Click again to restore camera.'
      : 'Host mode — camera will enumerate in a few seconds.';
    setTimeout(() => { toggleMsg.textContent = ''; }, 8000);
  }).catch(() => {
    toggleMsg.textContent = 'toggle failed — check server logs';
  }).finally(() => { toggleBtn.disabled = false; });
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
                "grid":     g,
                "raw_mm":   rm,
                "status":   _status[0],
                "usb_mode": get_usb_mode()
            }))
        elif self.path.startswith('/depth.mjpg'):
            self._stream_mjpeg()
        else:
            self._send(404, "text/plain", "not found")

    def _stream_mjpeg(self):
        if not _MJPEG:
            self._send(503, "text/plain",
                "pillow not installed — run: pip3 install pillow")
            return
        self.send_response(200)
        self.send_header("Content-Type",
            "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                with _jpg_lock:
                    jpg = _depth_jpg
                if jpg:
                    part = (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(jpg)).encode() + b"\r\n"
                        b"\r\n" + jpg + b"\r\n"
                    )
                    self.wfile.write(part)
                    self.wfile.flush()
                time.sleep(0.066)  # ~15 fps
        except Exception:
            pass

    def do_POST(self):
        if self.path == '/toggle':
            current  = get_usb_mode()
            new_mode = "device" if current == "host" else "host"
            try:
                subprocess.run(["sudo", "/usr/local/bin/usb-role", new_mode],
                    check=True, timeout=5, capture_output=True)
                svc_action = "enable" if new_mode == "host" else "disable"
                subprocess.run(["sudo", "systemctl", svc_action,
                    "usb-host-mode.service"],
                    capture_output=True, timeout=5)
                log(f"USB toggled -> {new_mode}")
            except Exception as e:
                log(f"toggle error: {e}")
            self._send(200, "application/json",
                json.dumps({"usb_mode": get_usb_mode()}))
        else:
            self._send(404, "text/plain", "not found")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == "__main__":
    if not _MJPEG:
        log("WARNING: pillow not found — /depth.mjpg unavailable. Fix: pip3 install pillow")
    t = threading.Thread(target=camera_loop, daemon=True)
    t.start()
    log(f"TactileSight server at http://10.221.208.1:8081  "
        f"(detect: 0-{DETECT_MM}mm, median/{HIST_FRAMES}f)")
    srv = ThreadedHTTPServer(("0.0.0.0", 8081), Handler)
    srv.serve_forever()
