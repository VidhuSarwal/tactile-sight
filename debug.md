# TactileSight — Debug Log

Running record of bugs discovered, root causes, and fixes applied across all sessions.

---

## BUG-001: Capture freezes depth camera feed
**Symptom:** After pressing Capture, depth MJPEG stream freezes; subsequent captures fail with camera reconnecting/failed errors.  
**Root cause:** `_do_capture()` ran inline inside the HTTP handler thread. It called `_snap_ready_evt.wait(timeout=3)` while holding the GIL, which blocked all other HTTP responses including the MJPEG stream loop. Additionally, the `_depth_jpg` global was being overwritten from two threads without a proper handoff.  
**Fix:** Extracted capture logic into a dedicated `capture_worker` daemon thread. HTTP `/capture` endpoint enqueues a timestamp via `queue.Queue(maxsize=1)` and returns immediately. `capture_worker` processes one capture at a time, serialising depth frame acquisition and RGB poll.  
**Commit:** `f2f1ff3`

---

## BUG-002: MJPEG stream race condition — base-chain keeps frame bytes alive
**Symptom:** Intermittent stale frames in the depth MJPEG stream; occasional off-by-one flicker.  
**Root cause:** Inside `_stream_mjpeg`, `arr = np.frombuffer(raw, ...)` creates a NumPy view that keeps a reference to the raw bytes object `raw`. If the GIL context-switches after the `np.frombuffer` call but before `del arr`, `frame_processor` is blocked from replacing `_depth_raw_mjpeg` with the next frame.  
**Fix:** Added `.copy()` to break the base-chain (`arr = np.frombuffer(...).copy()`) and immediately set `raw = None` after the copy, releasing the bytes reference before the 100ms encode loop. Also replaced `last_raw is raw` identity check (racy across threads) with `_depth_frame_count` monotonic counter.  
**Commit:** `f2f1ff3`

---

## BUG-003: SIGSEGV from glibc arena bloat (OOM — indirect)
**Symptom:** Service killed by SIGSEGV with 1.1 GB RSS peak; `journalctl` shows `status=11/SEGV` every ~2 minutes. Introduced in commit `22a1103`.  
**Root cause:** Commit `22a1103` moved MJPEG JPEG encoding off the camera thread into per-handler HTTP threads. Each new handler thread caused glibc to create its own 128 MB arena. With 8+ threads (default `MALLOC_ARENA_MAX = 8×CPU`), freed NumPy arrays (~614 KB each, 30 fps) accumulated in per-thread arenas without being returned to the OS. RSS grew at ~17.6 MB/s → OOM kill within 2 minutes.  
**Investigation:** `git show 22a1103` revealed OLD code stored ~60 KB JPEG in `_depth_jpg`; NEW code stored 614 KB raw bytes in `_depth_raw_mjpeg` and did 10 MB NumPy work per encode in each handler thread.  
**Fix:** Three environment variables added to `haptic-demo.service`:
```ini
Environment=MALLOC_ARENA_MAX=2
Environment=MALLOC_MMAP_THRESHOLD_=131072
Environment=MALLOC_TRIM_THRESHOLD_=131072
```
- `MALLOC_ARENA_MAX=2` — caps glibc arenas at 2, bounding freed-memory pool to ~2× one arena instead of 8×  
- `MALLOC_MMAP_THRESHOLD_=131072` — allocations >128 KB (all NumPy frames) use `mmap`; returned to OS on `free` via `munmap` instead of sitting in an arena  
- `MALLOC_TRIM_THRESHOLD_=131072` — triggers aggressive heap trim back to OS after frees  

**Result:** RSS peak dropped from 1.1 GB to ~390 MB.  
**Commit:** `f2f1ff3` (service file update) + manual `daemon-reload`

---

## BUG-004: MALLOC env vars not picked up after service crash-restart
**Symptom:** After the service crashes and systemd auto-restarts it, `/proc/PID/environ` shows no `MALLOC_*` variables. RSS climbs again.  
**Root cause:** `systemctl daemon-reload` was only run once; after that, crash-triggered auto-restarts use systemd's in-memory unit which may lag behind if the session was interrupted or a daemon-reload was missed.  
**Fix:** Run `systemctl daemon-reload && systemctl restart haptic-demo` whenever the service file changes. Verified with:
```bash
cat /proc/$(pgrep -f haptic_depth_server.py | head -1)/environ | tr '\0' '\n' | grep MALLOC
```
**Workaround ongoing:** Add reload to the deploy script so it is never missed.

---

## BUG-005: Recurring SIGSEGV at ~76 s — OpenNI2 internal C++ crash

### Why it crashes so frequently

**Symptom:** After BUG-003 fix, service still dies with SIGSEGV at ~76 s wall-time (24 s CPU time). RSS is actively *decreasing* (~400 MB → ~40 MB) before the crash — not an OOM. Systemd reports 390 MB *peak*, not current RSS.

**Root cause (confirmed working hypothesis):** The Orbbec libOpenNI2.so library has an internal bug that triggers a C++ SIGSEGV after approximately **2 300 depth frames** on this specific hardware + driver combination. At 30 fps: 2300 / 30 = **~76 seconds**.

The crash was **always present** in the original code, but was effectively hidden because the old `camera_loop` did PIL JPEG encoding synchronously inside the camera-reading loop. That encoding took ~50 ms per frame, naturally throttling the effective frame rate to ~15 fps. At 15 fps, 2300 frames takes ~153 seconds — long enough that most testing sessions never triggered it. After the frame pipeline refactor (camera_loop reads fast, frame_processor does the numpy work), the camera loop runs at full 30 fps and hits the 2300-frame threshold in half the time (~76 s).

**Pattern observed:**
- Every independent run crashes at 23–25 s CPU time → ~76–130 s wall time (varies with CPU load)
- RSS monotonically decreases before crash (MALLOC fix is working; crash is unrelated to memory)
- No kernel OOM entries; 2.5 GB RAM free
- No systemd memory limits (`MemoryMax=infinity`)
- SIGSEGV occurs inside C++ libOpenNI2.so (no Python exception, no Python traceback)

**Attempted mitigations:**
1. Frame header bounds check — prevents SIGSEGV from corrupted `data_size`; did not fix this crash (crash is elsewhere in OpenNI2)
2. 3 s USB settle delay — prevents startup SIGSEGV on rapid restart; did not extend the run window
3. `MALLOC_ARENA_MAX=2` — bounded memory but did not affect the crash timing

### Fix: subprocess isolation (`camera_reader.py`)

**Solution applied:** Moved all OpenNI2 frame-reading code into `linux/camera_reader.py`. This file runs as a **subprocess** of the main server (watchdog: `camera_subprocess_watchdog` thread).

**Architecture:**
```
haptic_depth_server.py (main process)
  ├── camera_subprocess_watchdog thread
  │     → spawns camera_reader.py
  │     → reads frames from subprocess stdout (8-byte header + raw bytes)
  │     → puts frames onto _raw_frame_q (same queue as before)
  │     → if subprocess crashes: waits 3s, restarts it
  ├── frame_processor thread  (unchanged — still reads from _raw_frame_q)
  ├── HTTP server
  └── WebSocket server
camera_reader.py (child process — contains all OpenNI2 ctypes code)
  → SIGSEGV here kills ONLY this process, not the main server
```

**User-visible effect after fix:**
- Depth stream pauses for ~6–8 s every ~76 s (camera_reader.py crashes and restarts)
- HTTP server, WebSocket, and haptic grid stay **continuously alive** — no 5 s full downtime
- Web UI remains responsive during the camera restart window

**Frame wire format (camera_reader.py → camera_subprocess_watchdog):**
```
[uint16 w][uint16 h][uint32 data_len][data_len bytes of raw uint16 depth]
= 8 bytes header + 614400 bytes per frame (640×480)
```

**Commit:** `[subprocess isolation commit]`

**Status:** Main server is now stable. Camera subprocess crashes and auto-recovers within ~6 s.

---

## BUG-006: Startup SIGSEGV when camera USB not ready (fast restart)
**Symptom:** After a crash, the very next restart sometimes dies within 2 s (63.9 MB peak, 1.921 s CPU). This is because the camera USB device takes ~3–5 s to reset after a disconnect.  
**Root cause:** When systemd restarts the process after only 5 s (`RestartSec=5`), `oniInitialize` or `oniDeviceOpen` is called while the camera's USB is still resetting. OpenNI2 segfaults during init rather than returning an error code.  
**Partial mitigation:** The retry loop in `camera_loop` catches `oniDeviceOpen` failures and sleeps 5 s, but the very first attempt (before any sleep) can hit the SIGSEGV during `oniInitialize`.  
**Planned fix:** Add a 3 s sleep at the top of `camera_loop` before the first `oniInitialize` call so the USB device has time to settle after the previous crash.

---

## BUG-007: Capture — first press misses RGB (camera warm-up latency)
**Symptom:** First capture after connecting RGB camera returns a depth PNG but an empty `rgb_b64` field.  
**Root cause:** `rgb_worker.py` calls `cv2.VideoCapture(0)` on first capture. Cold open takes ~500 ms; meanwhile the depth frame snapshot is taken and the 1 s poll for `rgb.jpg` expires.  
**Fix:** `rgb_worker.py` keeps the `VideoCapture` open between captures (opened once, reused). First capture now succeeds provided the camera is physically connected.  
**Status:** Resolved as part of capture-worker refactor.

---

## BUG-008: Logging not flushed on crash — "Many requests to capture are failing"
**Symptom:** Hard to diagnose why captures fail; no log output visible even when service was running.  
**Root cause:** `print()` output was being buffered; on a crash (SIGSEGV) the buffer was never flushed.  
**Fix:** `log = lambda m: print(f"[srv] {m}", flush=True)` — all log calls now use `flush=True`.  
**Status:** Resolved.

---

## BUG-009: The real cause of BUG-005 — malformed `oniFrameRelease` call (SUPERSEDES BUG-005)

**BUG-005's stated root cause is wrong.** There is no "internal bug in libOpenNI2.so that
triggers a SIGSEGV after ~2300 depth frames". The crash was heap corruption caused by our own
ctypes binding, and it is now fixed.

**Root cause:** The OpenNI2 C API is `void oniFrameRelease(OniFrame* pFrame)` — it takes the
frame pointer. The code passed `ctypes.byref(frame)`, i.e. the *address of the local ctypes
variable* (`OniFrame**`). OpenNI2 then decremented a refcount through a bogus pointer,
corrupting the heap. The crash point drifted with allocator state, which is exactly why it
looked like a frame-count threshold.

Note `oniStreamReadFrame(stream, ctypes.byref(frame))` is correct — that call genuinely takes
`OniFrame**`. Only the release call was wrong.

**Evidence:**
- glibc `double free or corruption (fasttop)`, then a reliable SIGSEGV after exactly 3 frames
  (1,843,224 bytes captured = 3 x 614,408).
- BUG-005's own note that `MALLOC_TRIM_THRESHOLD_` "may expose OpenNI2 UAF" was the right
  instinct — aggressive trimming made the corruption fault sooner.

**Fix:** pass the handle directly, and declare `argtypes` so ctypes cannot silently
mis-marshal it again:
```python
lib.oniFrameRelease.argtypes = [ctypes.c_void_p]   # OniFrame*, NOT OniFrame**
lib.oniFrameRelease(frame)                          # was: ctypes.byref(frame)
```
Applied at 3 call sites in `linux/camera_reader.py` and 3 in `linux/haptic_depth_server.py`
(the latter inside the now-dead inline `camera_loop`, kept consistent so re-enabling it is safe).

**Result:** 5,235 frames in 180 s at ~29 fps, no crash — straight past the 2,300 "ceiling".
Service soaked with **0** `reader exited` events.

**Consequence:** the subprocess isolation from BUG-005 is now belt-and-braces rather than
load-bearing. Keep it (it is still good defence), but the ~6-8 s stream gap every ~76 s that
BUG-005 lists as an expected user-visible effect **should no longer happen at all**. If it
reappears, something else is wrong.

---

## BUG-010: `setup.sh` provisions the USB unit in *device* mode

**Symptom:** After a fresh `setup.sh` run or reflash, `lsusb` is empty and `/grid` reports
`"waiting for camera"` forever. Boot dmesg shows xHCI enumerating the camera at t=5s, then
`xhci-hcd: remove` and `usb_vbus: disabling` at t=15s.

**Root cause:** `linux/setup.sh` step 5 wrote a unit named `usb-host-mode.service` whose
`ExecStart` was `sleep 2 && echo device > $ROLE_SYSFS`. Two faults: it wrote **device**, and a
2-second delay lands *before* the Qualcomm ADSP/OTG switch at ~16s, so the write is discarded
anyway. This contradicts `hard-fact.md`, which documents the unit as a 25-second delayed
oneshot that forces **host**.

**Fix:** `setup.sh` now emits `sleep 25 && /usr/local/bin/usb-role host`.

**To boot into device mode instead** (App Lab / ADB / WiFi setup), disable the unit rather than
editing it — `hard-fact.md` documents the web-UI toggle as doing exactly this.

---

## Key configuration facts for debugging

| Setting | Value | Why |
|---------|-------|-----|
| `MALLOC_ARENA_MAX` | `2` | Cap glibc arena count → bounded RSS |
| `MALLOC_MMAP_THRESHOLD_` | `131072` | >128 KB allocs use mmap → freed to OS on `free` |
| `MALLOC_TRIM_THRESHOLD_` | `131072` | Aggressive heap trim (may expose OpenNI2 UAF — see BUG-005) |
| `RestartSec` | `5` | Minimum time between crash-restarts |
| `StartLimitIntervalSec` | `0` | Unlimited restart attempts |
| `_raw_frame_q` maxsize | `2` | Drop frames when processor is behind; never stall OpenNI2 |
| `_capture_queue` maxsize | `1` | Latest capture request wins; parallel presses return "in progress" |

---

## Deploy checklist (to avoid BUG-004)

```bash
# Copy files
scp linux/haptic_depth_server.py arduino@10.221.208.1:~/
sudo cp linux/haptic-demo.service /etc/systemd/system/

# Always reload unit and restart
sudo systemctl daemon-reload
sudo systemctl restart haptic-demo

# Verify MALLOC vars in running process
cat /proc/$(pgrep -f haptic_depth_server.py | head -1)/environ | tr '\0' '\n' | grep MALLOC
```
