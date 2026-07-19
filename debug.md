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

## BUG-005: Remaining SIGSEGV at ~390 MB (non-OOM, under investigation)
**Symptom:** After BUG-003 fix, service still dies with SIGSEGV at ~76 s wall-time (24 s CPU time). RSS is actively *decreasing* (~400 MB → ~40 MB) before the crash — not an OOM. Systemd reports 390 MB *peak*, not current RSS.  
**Pattern observed:**
- Every independent run crashes at 23–25 s CPU time
- RSS monotonically decreases before crash
- No kernel OOM entries; 2.5 GB RAM free
- No systemd memory limits (`MemoryMax=infinity`)

**Hypotheses (unconfirmed):**
1. **MALLOC_TRIM_THRESHOLD_ exposes OpenNI2 use-after-free** — aggressive heap trimming (`sbrk(-size)`) unmaps memory that OpenNI2's internal C++ code still holds a pointer to. With default trim settings, the freed heap block stays mapped (in glibc's free list), masking the UAF. With 128 KB threshold, it is unmapped immediately → SIGSEGV on next access.  
2. **OpenNI2 internal frame-counter overflow** — crashes after ~2 300 frames (30 fps × 76 s), possibly a 12-bit or 16-bit internal counter.  
3. **USB host-mode frame desync** — UNO Q USB controller drops/corrupts a frame; OpenNI2 does not handle the bad frame gracefully.  

**Mitigation applied (BUG-005a):** Added strict bounds validation on frame header values before `ctypes.string_at`. Prevents a corrupted `data_size` field from requesting a read into unmapped memory:
```python
MAX_FRAME = 640 * 480 * 2  # 614400 bytes
if (data_addr and 0 < data_size <= MAX_FRAME
        and 0 < w <= 640 and 0 < h <= 480):
    raw = ctypes.string_at(data_addr, data_size)
```
**Status:** SIGSEGV still occurs; root cause not conclusively identified. `Restart=always` + `RestartSec=5` ensure service recovers within 5 s. Subprocess isolation (running camera_loop in a child process so SIGSEGV does not kill the HTTP/WS server) is the planned permanent fix.

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
