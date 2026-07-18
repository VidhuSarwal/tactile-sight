#!/usr/bin/env python3
"""
TactileSight rgb_worker — cv2 lives here, safely isolated from OpenNI2 ctypes.
Polls /dev/shm/tactile/trigger.ts; on new timestamp, writes a fresh 640x480
JPEG to /dev/shm/tactile/rgb.jpg (atomic via .tmp + rename).
"""
import os, time, sys
import cv2

SHM     = '/dev/shm/tactile'
TRIGGER = os.path.join(SHM, 'trigger.ts')
OUT_TMP = os.path.join(SHM, 'rgb.jpg.tmp')
OUT     = os.path.join(SHM, 'rgb.jpg')

os.makedirs(SHM, exist_ok=True)

cap = None
last_trigger = 0.0

def open_camera():
    global cap
    if cap is not None:
        cap.release()
    c = cv2.VideoCapture('/dev/video0', cv2.CAP_V4L2)
    c.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    c.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    c.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    if not c.isOpened():
        c.release()
        return False
    cap = c
    print(f"[rgb] camera open: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
          f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}", flush=True)
    return True

print("[rgb] rgb_worker starting", flush=True)

while True:
    # Open camera (retry if unavailable)
    if cap is None or not cap.isOpened():
        if not open_camera():
            print("[rgb] /dev/video0 unavailable — retrying in 5s", flush=True)
            time.sleep(5)
            continue

    ret, frame = cap.read()
    if not ret or frame is None:
        print("[rgb] frame read failed — reopening", flush=True)
        cap.release()
        cap = None
        time.sleep(1)
        continue

    # Check trigger
    try:
        ts = float(open(TRIGGER).read().strip())
        if ts > last_trigger:
            ok, jpg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                with open(OUT_TMP, 'wb') as f:
                    f.write(jpg.tobytes())
                os.rename(OUT_TMP, OUT)
                print(f"[rgb] snapshot written ({len(jpg.tobytes())//1024}KB)", flush=True)
            last_trigger = ts
    except Exception:
        pass

    time.sleep(0.05)   # 20Hz drain — ~50ms response on trigger
