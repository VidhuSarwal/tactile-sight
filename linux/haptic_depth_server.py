#!/usr/bin/env python3
"""
TactileSight depth->haptic server  v5
  GET /          -> web UI (haptic grid + all toggles)
  GET /grid      -> JSON {grid, raw_mm, status, usb_mode, yolo_active, yolo_fps, detections}
  GET /stats     -> JSON {haptic_fps, yolo_fps, yolo_latency_ms, ram_used_mb, tune, ...}
  GET /depth.mjpg -> colorized depth MJPEG (only encodes when browser is connected)
  POST /toggle        -> flip USB-C host<->device
  POST /yolo/toggle   -> start / stop YOLO worker
  POST /yolo/resolution  body {"size":320|640} -> restart worker at new resolution
  POST /tune     body {"detect_mm":1500, "fov_left":10, ...} -> live-adjust sensing params
  Port 8081

Sensing: 21 cells (3×7). Always-on depth safety net. YOLO semantic layer is optional.
cv2 / ONNX inference run in a separate subprocess (yolo_worker.py) — required to avoid
the cv2+numpy+OpenNI ctypes SIGSEGV bug.
"""
import os, gc, ctypes, threading, struct, time, json, subprocess
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
COLS, ROWS  = 7, 3
N_CELLS     = COLS * ROWS
HIST_FRAMES = 5
MIN_VALID_FRAC = 0.08

# --- Defaults (overridable via /tune) ---
DETECT_MM_DEFAULT    = 2000
NEAR_FLOOR_MM_DEFAULT = 350
CELL_PCT_DEFAULT     = 20

# ── Shared state ──────────────────────────────────────────────────────────────

_grid_lock = threading.Lock()
_grid      = [0.0] * N_CELLS
_raw_mm    = [0]   * N_CELLS
_status    = ["starting..."]

_hist   = [[float(DETECT_MM_DEFAULT)] * HIST_FRAMES for _ in range(N_CELLS)]
_hist_i = 0

# Live-tunable sensing parameters
_tune_lock = threading.Lock()
_tune = {
    "detect_mm":       float(DETECT_MM_DEFAULT),
    "near_floor_mm":   float(NEAR_FLOOR_MM_DEFAULT),
    "cell_percentile": float(CELL_PCT_DEFAULT),
    "fov_left":        0.0,    # % of frame width to trim on left  (0-40)
    "fov_right":       100.0,  # % of frame width to include up to (60-100)
    "fov_top":         0.0,    # % of frame height to trim on top  (0-40)
    "fov_bottom":      100.0,  # % of frame height to include down to (60-100)
}

# MJPEG depth stream
_jpg_lock       = threading.Lock()
_depth_jpg      = None
_mjpeg_cli_lock = threading.Lock()
_mjpeg_clients  = 0

# Raw frame shared between camera_loop and the MJPEG encoder thread.
# camera_loop drops a copy here (fast); encoder thread picks it up and encodes (slow).
# This keeps JPEG work off the depth processing thread entirely.
_raw_frame_lock = threading.Lock()
_raw_frame_d    = None

# YOLO worker
_yolo_lock    = threading.Lock()
_yolo_proc    = None    # subprocess.Popen or None
_yolo_active  = False
_yolo_size    = 320
_yolo_fps     = 0.0
_yolo_lat_ms  = 0.0

_det_lock    = threading.Lock()
_detections  = []       # [{cell, class, conf, dist_mm}, ...]

# Haptic FPS
_haptic_fps    = 0.0
_hfps_count    = 0
_hfps_ts       = time.time()

log = lambda m: print(f"[srv] {m}", flush=True)


def get_usb_mode():
    try:
        return open(ROLE_PATH).read().strip()
    except Exception:
        return "unknown"


def get_ram_mb():
    try:
        for line in open("/proc/self/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def get_stats():
    with _tune_lock:
        tune = dict(_tune)
    with _mjpeg_cli_lock:
        clients = _mjpeg_clients
    return {
        "haptic_fps":      round(_haptic_fps, 1),
        "yolo_fps":        round(_yolo_fps, 1),
        "yolo_latency_ms": round(_yolo_lat_ms, 1),
        "ram_used_mb":     get_ram_mb(),
        "mjpeg_clients":   clients,
        "yolo_active":     _yolo_active,
        "yolo_size":       _yolo_size,
        "tune":            tune,
    }


# ── Colorized depth (MJPEG) ──────────────────────────────────────────────────

def colorize_depth(d_u16):
    """Near=red, far=green, blind zone (<350mm)=grey, invalid=black."""
    with _tune_lock:
        detect = _tune["detect_mm"]
        near   = _tune["near_floor_mm"]
    valid = (d_u16 >= near) & (d_u16 < detect)
    blind = d_u16 < near
    norm  = np.where(valid,
        np.clip((d_u16.astype(np.float32) - near) / max(1, detect - near), 0, 1),
        0.0)
    r = np.where(valid, np.clip((1.0 - norm) * 255, 0, 255), 0).astype(np.uint8)
    g = np.where(valid, np.clip(norm * 220, 0, 255), 0).astype(np.uint8)
    b = np.zeros(d_u16.shape, dtype=np.uint8)
    r[blind] = 70; g[blind] = 70; b[blind] = 70
    return np.stack([r, g, b], axis=2)


# ── MJPEG encoder thread ─────────────────────────────────────────────────────

def mjpeg_encoder_loop():
    """
    Encodes colorized depth frames in a dedicated thread so camera_loop is never
    blocked by PIL/JPEG work (~15ms per frame on Kryo 260).
    Runs at 10fps (100ms sleep) — enough for a debug view.
    """
    global _depth_jpg
    while True:
        with _mjpeg_cli_lock:
            has_clients = _mjpeg_clients > 0

        if not has_clients:
            time.sleep(0.1)
            continue

        with _raw_frame_lock:
            d = _raw_frame_d  # just grab the reference (numpy array or None)

        if d is None:
            time.sleep(0.1)
            continue

        try:
            rgb = colorize_depth(d)
            img = Image.fromarray(rgb, 'RGB')
            buf = _io.BytesIO()
            img.save(buf, format='JPEG', quality=50)
            with _jpg_lock:
                _depth_jpg = buf.getvalue()
        except Exception:
            pass

        time.sleep(0.1)  # 10fps


# ── YOLO bbox → grid cell mapping ────────────────────────────────────────────

def map_bbox_to_cell(cx, cy, img_w, img_h):
    """Map a detection center (in original image pixels) to a 3×7 cell index."""
    with _tune_lock:
        fl = _tune["fov_left"]   / 100.0
        fr = _tune["fov_right"]  / 100.0
        ft = _tune["fov_top"]    / 100.0
        fb = _tune["fov_bottom"] / 100.0
    fov_w = fr - fl; fov_h = fb - ft
    if fov_w <= 0 or fov_h <= 0:
        return None
    x_rel = (cx / img_w - fl) / fov_w
    y_rel = (cy / img_h - ft) / fov_h
    if not (0.0 <= x_rel < 1.0 and 0.0 <= y_rel < 1.0):
        return None
    col = max(0, min(COLS - 1, int(x_rel * COLS)))
    row = max(0, min(ROWS - 1, int(y_rel * ROWS)))
    return row * COLS + col


# ── YOLO worker process management ───────────────────────────────────────────

def start_yolo_worker(size):
    global _yolo_proc, _yolo_active, _yolo_size, _yolo_fps, _yolo_lat_ms
    stop_yolo_worker()
    model = os.path.expanduser(f"~/models/yolov8n_{size}.onnx")
    if not os.path.exists(model):
        log(f"YOLO model missing: {model}")
        return False
    worker = os.path.expanduser("~/yolo_worker.py")
    if not os.path.exists(worker):
        log(f"yolo_worker.py missing: {worker}")
        return False
    try:
        proc = subprocess.Popen(
            ["taskset", "-c", "2,3", "python3", worker,
             "--size", str(size), "--model", model],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, bufsize=1
        )
        with _yolo_lock:
            _yolo_proc   = proc
            _yolo_active = True
            _yolo_size   = size
        log(f"YOLO worker started (size={size}, pid={proc.pid})")
        return True
    except Exception as e:
        log(f"YOLO start error: {e}")
        return False


def stop_yolo_worker():
    global _yolo_proc, _yolo_active, _yolo_fps, _yolo_lat_ms
    with _yolo_lock:
        p            = _yolo_proc
        _yolo_proc   = None
        _yolo_active = False
        _yolo_fps    = 0.0
        _yolo_lat_ms = 0.0
    with _det_lock:
        _detections.clear()
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()
    log("YOLO worker stopped")


def det_reader_loop():
    """Background thread: reads YOLO worker stdout, maps detections to grid cells."""
    global _yolo_fps, _yolo_lat_ms, _yolo_active
    while True:
        with _yolo_lock:
            proc = _yolo_proc
        if proc is None:
            time.sleep(0.1)
            continue

        # Read from this specific proc until it exits or is replaced
        while True:
            with _yolo_lock:
                if _yolo_proc is not proc:
                    break
            try:
                line = proc.stdout.readline()
            except Exception:
                break
            if not line:
                # EOF — process died
                with _yolo_lock:
                    if _yolo_proc is proc:
                        _yolo_active = False
                        log("YOLO worker pipe EOF — died unexpectedly")
                break

            try:
                data = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            if "dets" not in data:
                continue

            ts_recv      = time.perf_counter()
            _yolo_lat_ms = max(0.0, (ts_recv - data.get("ts", ts_recv)) * 1000)
            _yolo_fps    = data.get("fps", 0.0)
            w_img        = data.get("w", 640)
            h_img        = data.get("h", 480)

            new_dets = []
            for det in data["dets"]:
                cx, cy, bw, bh, cls_id, cls_name, conf = det
                cell = map_bbox_to_cell(cx, cy, w_img, h_img)
                if cell is not None:
                    with _grid_lock:
                        dist_mm = _raw_mm[cell]
                    new_dets.append({
                        "cell":    cell,
                        "class":   cls_name,
                        "conf":    round(conf, 2),
                        "dist_mm": dist_mm
                    })
            with _det_lock:
                _detections[:] = new_dets

        time.sleep(0.1)


# ── Depth processing ──────────────────────────────────────────────────────────

def process_frame(frame_bytes, w, h):
    global _hist_i, _depth_jpg, _haptic_fps, _hfps_count, _hfps_ts

    # Snapshot tune params once (avoids lock contention in inner loops)
    with _tune_lock:
        detect_mm  = _tune["detect_mm"]
        near_floor = _tune["near_floor_mm"]
        cell_pct   = _tune["cell_percentile"]
        x_start    = int(w * _tune["fov_left"]   / 100)
        x_end      = int(w * _tune["fov_right"]  / 100)
        y_start    = int(h * _tune["fov_top"]    / 100)
        y_end      = int(h * _tune["fov_bottom"] / 100)

    d = np.frombuffer(frame_bytes, dtype=np.uint16).reshape(h, w)

    # FOV crop — defines the active sensing zone
    d_crop = d[y_start:y_end, x_start:x_end]
    ch     = max(1, (y_end - y_start) // ROWS)
    cw     = max(1, (x_end - x_start) // COLS)

    for idx in range(N_CELLS):
        r, c  = divmod(idx, COLS)
        cell  = d_crop[r*ch:(r+1)*ch, c*cw:(c+1)*cw]
        valid = cell[cell >= near_floor]
        min_px = max(5, int(cell.size * MIN_VALID_FRAC))
        if valid.size < min_px:
            _hist[idx][_hist_i % HIST_FRAMES] = float(detect_mm)
        else:
            _hist[idx][_hist_i % HIST_FRAMES] = float(np.percentile(valid, cell_pct))

    _hist_i += 1

    levels, raw_out = [], []
    for idx in range(N_CELLS):
        med = float(np.median(_hist[idx]))
        if med >= detect_mm:
            levels.append(0.0); raw_out.append(0)
        else:
            level = (detect_mm - med) / detect_mm * 255.0
            levels.append(min(255.0, max(0.0, level))); raw_out.append(int(med))

    # Haptic FPS tracking
    _hfps_count += 1
    now = time.time()
    if now - _hfps_ts >= 1.0:
        _haptic_fps = _hfps_count / (now - _hfps_ts)
        _hfps_count = 0
        _hfps_ts    = now

    # Hand a copy of the raw frame to the MJPEG encoder thread.
    # The copy (~0.3ms) is all we do here — actual JPEG encoding happens off this thread.
    if _MJPEG:
        with _mjpeg_cli_lock:
            has_clients = _mjpeg_clients > 0
        if has_clients:
            with _raw_frame_lock:
                _raw_frame_d = d.copy()

    # Periodic GC to release allocator-held pages from numpy operations
    if _hist_i % 100 == 0:
        gc.collect()

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
            log(f"depth->haptic active | {HIST_FRAMES}-frame median"
                + (" | MJPEG ready" if _MJPEG else ""))

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


# ── HTML ──────────────────────────────────────────────────────────────────────

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
         display: flex; flex-direction: column; align-items: center;
         justify-content: center; font-size: 8px; line-height: 1.2;
         color: rgba(255,255,255,0.75); user-select: none; text-align: center; }
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
  .meta-row { margin-top: 14px; display: flex; flex-wrap: wrap; gap: 14px;
              justify-content: center; }
  .meta-link { font-size: 0.72rem; color: #444; cursor: pointer; user-select: none; }
  .meta-link:hover { color: #888; }
  .meta-link.active { color: #7af; }
  #depth-wrap { margin-top: 16px; display: none; }
  #depth-img  { max-width: 480px; width: 100%; border: 1px solid #2a2a2a;
                border-radius: 4px; display: block; }
  #depth-label { font-size: 0.65rem; color: #444; text-align: center; margin-top: 4px; }

  /* Stats panel */
  #stats-wrap { display: none; margin-top: 14px; font-size: 0.72rem; }
  .stats-grid { display: grid; grid-template-columns: repeat(4, auto);
                gap: 4px 18px; }
  .stats-key { color: #555; }
  .stats-val { color: #9cf; font-weight: bold; }

  /* Tune panel */
  #tune-wrap { display: none; margin-top: 14px; width: 100%; max-width: 460px; }
  .tune-row { display: flex; align-items: center; gap: 8px;
              margin-bottom: 8px; font-size: 0.72rem; }
  .tune-row label { color: #666; width: 130px; flex-shrink: 0; }
  .tune-row input[type=range] { flex: 1; accent-color: #7af; cursor: pointer; }
  .tune-val { color: #9cf; width: 52px; text-align: right; flex-shrink: 0; }
  .tune-section { color: #555; font-size: 0.65rem; letter-spacing: 2px;
                  text-transform: uppercase; margin: 10px 0 4px; }
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

<div class="meta-row">
  <span class="meta-link" id="mm-link"    onclick="toggleMm()">[ show mm ]</span>
  <span class="meta-link" id="depth-link" onclick="toggleDepth()">[ depth cam ]</span>
  <span class="meta-link" id="yolo-link"  onclick="doYoloToggle()">[ YOLO: off ]</span>
  <span class="meta-link" id="res-link"   onclick="doResToggle()" style="display:none">[ 320px ]</span>
  <span class="meta-link" id="stats-link" onclick="toggleStats()">[ stats ]</span>
  <span class="meta-link" id="tune-link"  onclick="toggleTune()">[ tune ]</span>
</div>

<div id="depth-wrap">
  <img id="depth-img" alt="depth stream" />
  <div id="depth-label">red=near &nbsp;|&nbsp; yellow/green=far &nbsp;|&nbsp; grey=blind zone (&lt;350mm) &nbsp;|&nbsp; black=no return</div>
</div>

<div id="stats-wrap">
  <div class="stats-grid" id="stats-grid"></div>
</div>

<div id="tune-wrap">
  <div class="tune-section">Range</div>
  <div class="tune-row">
    <label>Detect range</label>
    <input type="range" id="t-detect" min="500" max="3000" step="100"
           oninput="tune('detect_mm',this.value,'v-detect','mm')">
    <span class="tune-val"><span id="v-detect">2000</span>mm</span>
  </div>
  <div class="tune-row">
    <label>Near cutoff</label>
    <input type="range" id="t-near" min="100" max="700" step="25"
           oninput="tune('near_floor_mm',this.value,'v-near','mm')">
    <span class="tune-val"><span id="v-near">350</span>mm</span>
  </div>
  <div class="tune-row">
    <label>Sensitivity %ile</label>
    <input type="range" id="t-pct" min="5" max="50" step="5"
           oninput="tune('cell_percentile',this.value,'v-pct','')">
    <span class="tune-val"><span id="v-pct">20</span></span>
  </div>

  <div class="tune-section">Horizontal FOV (view angle)</div>
  <div class="tune-row">
    <label>Left trim %</label>
    <input type="range" id="t-fl" min="0" max="40" step="5"
           oninput="tune('fov_left',this.value,'v-fl','%')">
    <span class="tune-val"><span id="v-fl">0</span>%</span>
  </div>
  <div class="tune-row">
    <label>Right trim %</label>
    <input type="range" id="t-fr" min="60" max="100" step="5"
           oninput="tune('fov_right',this.value,'v-fr','%')">
    <span class="tune-val"><span id="v-fr">100</span>%</span>
  </div>

  <div class="tune-section">Vertical FOV</div>
  <div class="tune-row">
    <label>Top trim %</label>
    <input type="range" id="t-ft" min="0" max="40" step="5"
           oninput="tune('fov_top',this.value,'v-ft','%')">
    <span class="tune-val"><span id="v-ft">0</span>%</span>
  </div>
  <div class="tune-row">
    <label>Bottom trim %</label>
    <input type="range" id="t-fb" min="60" max="100" step="5"
           oninput="tune('fov_bottom',this.value,'v-fb','%')">
    <span class="tune-val"><span id="v-fb">100</span>%</span>
  </div>
</div>

<script>
const statusEl   = document.getElementById('status');
const modeBadge  = document.getElementById('mode-badge');
const toggleBtn  = document.getElementById('toggle-btn');
const toggleMsg  = document.getElementById('toggle-msg');
const gridEl     = document.getElementById('grid');
const depthWrap  = document.getElementById('depth-wrap');
const depthImg   = document.getElementById('depth-img');
const depthLink  = document.getElementById('depth-link');
const yoloLink   = document.getElementById('yolo-link');
const resLink    = document.getElementById('res-link');
const statsWrap  = document.getElementById('stats-wrap');
const statsGrid  = document.getElementById('stats-grid');
const tuneWrap   = document.getElementById('tune-wrap');

let showMm    = false;
let depthOn   = false;
let yoloOn    = false;
let yoloSize  = 320;
let showStats = false;
let showTune  = false;
let lastRaw   = [];
let lastDets  = [];
let _tuneDebounce = null;

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

function toggleMm() {
  showMm = !showMm;
  document.getElementById('mm-link').textContent = showMm ? '[ hide mm ]' : '[ show mm ]';
}

function toggleDepth() {
  depthOn = !depthOn;
  if (depthOn) {
    depthWrap.style.display = 'block';
    depthImg.src = '/depth.mjpg?' + Date.now();
    depthLink.textContent = '[ hide depth ]';
  } else {
    depthWrap.style.display = 'none';
    depthImg.src = '';
    depthLink.textContent = '[ depth cam ]';
  }
}

function toggleStats() {
  showStats = !showStats;
  statsWrap.style.display = showStats ? 'block' : 'none';
  document.getElementById('stats-link').classList.toggle('active', showStats);
  if (showStats) pollStats();
}

function toggleTune() {
  showTune = !showTune;
  tuneWrap.style.display = showTune ? 'block' : 'none';
  document.getElementById('tune-link').classList.toggle('active', showTune);
  if (showTune) pollStats();  // pull current values
}

function applyTuneValues(t) {
  if (!t) return;
  const set = (id, slider, val, suffix) => {
    document.getElementById(id).textContent = Math.round(val);
    const sl = document.getElementById(slider);
    if (sl && document.activeElement !== sl) sl.value = val;
  };
  set('v-detect', 't-detect', t.detect_mm, 'mm');
  set('v-near',   't-near',   t.near_floor_mm, 'mm');
  set('v-pct',    't-pct',    t.cell_percentile, '');
  set('v-fl',     't-fl',     t.fov_left, '%');
  set('v-fr',     't-fr',     t.fov_right, '%');
  set('v-ft',     't-ft',     t.fov_top, '%');
  set('v-fb',     't-fb',     t.fov_bottom, '%');
}

function tune(param, value, spanId, suffix) {
  document.getElementById(spanId).textContent = Math.round(value);
  if (_tuneDebounce) clearTimeout(_tuneDebounce);
  _tuneDebounce = setTimeout(() => {
    fetch('/tune', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({[param]: parseFloat(value)})
    });
  }, 80);
}

function pollStats() {
  fetch('/stats').then(r => r.json()).then(s => {
    if (showStats) {
      const rows = [
        ['haptic fps', s.haptic_fps],
        ['yolo fps',   s.yolo_fps],
        ['yolo latency', s.yolo_latency_ms + 'ms'],
        ['ram used', s.ram_used_mb + ' MB'],
        ['mjpeg clients', s.mjpeg_clients],
        ['yolo size', s.yolo_size + 'px'],
      ];
      statsGrid.innerHTML = rows.map(([k,v]) =>
        `<span class="stats-key">${k}</span><span class="stats-val">${v}</span>`
      ).join('');
    }
    if (showTune && s.tune) applyTuneValues(s.tune);
    if (showStats || showTune) setTimeout(pollStats, 500);
  }).catch(() => {
    if (showStats || showTune) setTimeout(pollStats, 1000);
  });
}

function doYoloToggle() {
  fetch('/yolo/toggle', {method:'POST'}).then(r => r.json()).then(d => {
    yoloOn = d.yolo_active;
    updateYoloUI();
  });
}

function doResToggle() {
  yoloSize = yoloSize === 320 ? 640 : 320;
  fetch('/yolo/resolution', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({size: yoloSize})
  }).then(r => r.json()).then(d => {
    yoloSize = d.yolo_size || yoloSize;
    resLink.textContent = `[ ${yoloSize}px ]`;
  });
}

function updateYoloUI() {
  if (yoloOn) {
    yoloLink.textContent = '[ YOLO: on ]';
    yoloLink.classList.add('active');
    resLink.style.display = 'inline';
    resLink.textContent = `[ ${yoloSize}px ]`;
  } else {
    yoloLink.textContent = '[ YOLO: off ]';
    yoloLink.classList.remove('active');
    resLink.style.display = 'none';
  }
}

function poll() {
  fetch('/grid').then(r => r.json()).then(data => {
    const g   = data.grid || [];
    lastRaw   = data.raw_mm || [];
    lastDets  = data.detections || [];
    yoloOn    = data.yolo_active || false;
    const fps = data.yolo_fps || 0;

    // Build detection lookup by cell
    const detByCell = {};
    for (const d of lastDets) detByCell[d.cell] = d;

    for (let i = 0; i < 21; i++) {
      const s = levelToStyle(g[i] || 0);
      dots[i].style.background = s.bg;
      dots[i].style.boxShadow  = s.shadow;
      const sc = 1 + ((g[i] || 0) / 255) * 0.28;
      dots[i].style.transform  = `scale(${sc.toFixed(2)})`;

      // Label: YOLO class takes priority; fall back to mm in debug mode
      if (yoloOn && detByCell[i]) {
        const det = detByCell[i];
        const dist = det.dist_mm > 0 ? (det.dist_mm/1000).toFixed(1)+'m' : '';
        dots[i].textContent = det.class + (dist ? '\n' + dist : '');
      } else if (showMm && lastRaw[i]) {
        dots[i].textContent = lastRaw[i] + 'mm';
      } else {
        dots[i].textContent = '';
      }
    }

    // YOLO link FPS annotation
    if (yoloOn) {
      yoloLink.textContent = fps > 0 ? `[ YOLO: ${fps}fps ]` : '[ YOLO: on ]';
      yoloLink.classList.add('active');
      resLink.style.display = 'inline';
    } else {
      yoloLink.textContent = '[ YOLO: off ]';
      yoloLink.classList.remove('active');
      resLink.style.display = 'none';
    }

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


# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def _send(self, code, ctype, body):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b"{}"

    def do_GET(self):
        if self.path == '/':
            self._send(200, "text/html; charset=utf-8", HTML)

        elif self.path == '/grid':
            with _grid_lock:
                g  = list(_grid)
                rm = list(_raw_mm)
            with _det_lock:
                dets = list(_detections)
            self._send(200, "application/json", json.dumps({
                "grid":       g,
                "raw_mm":     rm,
                "status":     _status[0],
                "usb_mode":   get_usb_mode(),
                "yolo_active": _yolo_active,
                "yolo_fps":   round(_yolo_fps, 1),
                "detections": dets,
            }))

        elif self.path == '/stats':
            self._send(200, "application/json", json.dumps(get_stats()))

        elif self.path.startswith('/depth.mjpg'):
            self._stream_mjpeg()

        else:
            self._send(404, "text/plain", "not found")

    def _stream_mjpeg(self):
        global _mjpeg_clients
        if not _MJPEG:
            self._send(503, "text/plain", "pillow not installed — run: pip3 install pillow")
            return
        with _mjpeg_cli_lock:
            _mjpeg_clients += 1
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                with _jpg_lock:
                    jpg = _depth_jpg
                if jpg:
                    part = (b"--frame\r\nContent-Type: image/jpeg\r\n"
                            b"Content-Length: " + str(len(jpg)).encode()
                            + b"\r\n\r\n" + jpg + b"\r\n")
                    self.wfile.write(part)
                    self.wfile.flush()
                time.sleep(0.066)
        except Exception:
            pass
        finally:
            with _mjpeg_cli_lock:
                _mjpeg_clients -= 1

    def do_POST(self):
        global _yolo_size
        if self.path == '/toggle':
            current  = get_usb_mode()
            new_mode = "device" if current == "host" else "host"
            try:
                subprocess.run(["sudo", "/usr/local/bin/usb-role", new_mode],
                    check=True, timeout=5, capture_output=True)
                svc = "enable" if new_mode == "host" else "disable"
                subprocess.run(["sudo", "systemctl", svc, "usb-host-mode.service"],
                    capture_output=True, timeout=5)
                log(f"USB toggled -> {new_mode}")
            except Exception as e:
                log(f"toggle error: {e}")
            self._send(200, "application/json",
                json.dumps({"usb_mode": get_usb_mode()}))

        elif self.path == '/yolo/toggle':
            if _yolo_active:
                stop_yolo_worker()
                self._send(200, "application/json",
                    json.dumps({"yolo_active": False}))
            else:
                ok = start_yolo_worker(_yolo_size)
                self._send(200, "application/json",
                    json.dumps({"yolo_active": ok, "yolo_size": _yolo_size}))

        elif self.path == '/yolo/resolution':
            try:
                data = json.loads(self._read_body())
                size = int(data.get("size", 320))
                if size not in (320, 640):
                    raise ValueError("size must be 320 or 640")
                _yolo_size = size
                if _yolo_active:
                    start_yolo_worker(size)
                self._send(200, "application/json",
                    json.dumps({"yolo_size": size}))
            except Exception as e:
                self._send(400, "application/json", json.dumps({"error": str(e)}))

        elif self.path == '/tune':
            try:
                data = json.loads(self._read_body())
                valid_keys = {"detect_mm", "near_floor_mm", "cell_percentile",
                              "fov_left", "fov_right", "fov_top", "fov_bottom"}
                with _tune_lock:
                    for k, v in data.items():
                        if k in valid_keys:
                            _tune[k] = float(v)
                self._send(200, "application/json", json.dumps(get_stats()))
            except Exception as e:
                self._send(400, "application/json", json.dumps({"error": str(e)}))

        else:
            self._send(404, "text/plain", "not found")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not _MJPEG:
        log("WARNING: pillow not found — /depth.mjpg disabled. Fix: pip3 install pillow")

    threading.Thread(target=camera_loop,      daemon=True).start()
    threading.Thread(target=det_reader_loop,  daemon=True).start()
    threading.Thread(target=mjpeg_encoder_loop, daemon=True).start()

    log(f"TactileSight v5 at http://10.221.208.1:8081")
    log(f"  depth safety net: always on")
    log(f"  YOLO: toggle via UI or POST /yolo/toggle")
    log(f"  tune: POST /tune  |  stats: GET /stats")
    srv = ThreadedHTTPServer(("0.0.0.0", 8081), Handler)
    srv.serve_forever()
