#!/usr/bin/env python3
"""
TactileSight YOLO worker — RGB capture + ONNX inference → JSON stdout
Runs as a subprocess from haptic_depth_server.py.
cv2 is safely isolated here (no OpenNI ctypes conflict).

IPC protocol (stdout, one JSON line per frame):
  {"dets": [[cx,cy,w,h,cls_id,cls_name,conf], ...], "fps": 8.3, "ts": 1234.5, "w": 640, "h": 480}
  {"status": "waiting", "msg": "..."}   — camera unavailable / retry
  {"status": "model_loaded"}
  {"status": "camera_open", "cap_w": N, "cap_h": N}

Logs go to stderr. Never write debug output to stdout.

Usage:
  python3 yolo_worker.py --size 320|640 --model /home/arduino/models/yolov8n_320.onnx
"""
import sys, os, time, json, signal, argparse
import numpy as np
import cv2
import onnxruntime as ort

COCO_NAMES = [
    "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat",
    "traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat",
    "dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack",
    "umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball",
    "kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket",
    "bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple",
    "sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair",
    "couch","potted plant","bed","dining table","toilet","tv","laptop","mouse",
    "remote","keyboard","cell phone","microwave","oven","toaster","sink",
    "refrigerator","book","clock","vase","scissors","teddy bear","hair drier",
    "toothbrush"
]

CONF_THRESH = 0.40
NMS_THRESH  = 0.45

_stop = False

def _sig(sig, frame):
    global _stop
    _stop = True

def emit(obj):
    """Write JSON line to stdout (IPC channel)."""
    print(json.dumps(obj), flush=True)

def err(msg):
    print(f"[yolo] {msg}", file=sys.stderr, flush=True)


def letterbox(img, size):
    """Resize + pad to size×size maintaining aspect ratio. Returns (padded, scale, pad_x, pad_y)."""
    h, w = img.shape[:2]
    scale = min(size / w, size / h)
    nw, nh = int(w * scale), int(h * scale)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas  = np.full((size, size, 3), 114, dtype=np.uint8)
    pad_x   = (size - nw) // 2
    pad_y   = (size - nh) // 2
    canvas[pad_y:pad_y+nh, pad_x:pad_x+nw] = resized
    return canvas, scale, pad_x, pad_y


def decode(raw_out, size, scale, pad_x, pad_y):
    """
    Decode YOLOv8 ONNX output [1, 84, N] to list of detection tuples.
    Each tuple: [cx, cy, w, h, cls_id, cls_name, conf]
    cx/cy/w/h are in original (pre-letterbox) image pixels.
    """
    preds     = raw_out[0].squeeze(0).T   # [N, 84]
    boxes     = preds[:, :4]              # cx,cy,w,h (in letterbox coords)
    cls_scores = preds[:, 4:]             # [N, 80]

    cls_ids   = cls_scores.argmax(axis=1)
    cls_confs = cls_scores.max(axis=1)

    keep = cls_confs >= CONF_THRESH
    if not keep.any():
        return []

    boxes, cls_ids, cls_confs = boxes[keep], cls_ids[keep], cls_confs[keep]

    # cx,cy,w,h → x1,y1,w,h for NMSBoxes
    cx, cy, bw, bh = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    x1 = np.clip(cx - bw / 2, 0, size)
    y1 = np.clip(cy - bh / 2, 0, size)
    bw = np.clip(bw, 0, size)
    bh = np.clip(bh, 0, size)

    nms_boxes = np.stack([x1, y1, bw, bh], axis=1).tolist()
    nms_confs = cls_confs.tolist()
    idxs = cv2.dnn.NMSBoxes(nms_boxes, nms_confs, CONF_THRESH, NMS_THRESH)
    if len(idxs) == 0:
        return []
    if isinstance(idxs, np.ndarray):
        idxs = idxs.flatten().tolist()

    results = []
    for i in idxs:
        # Map center back to original image coords (undo letterbox)
        ocx = (cx[i] - pad_x) / scale
        ocy = (cy[i] - pad_y) / scale
        obw = bw[i] / scale
        obh = bh[i] / scale
        cid   = int(cls_ids[i])
        cname = COCO_NAMES[cid] if cid < len(COCO_NAMES) else str(cid)
        conf  = float(cls_confs[i])
        results.append([float(ocx), float(ocy), float(obw), float(obh), cid, cname, conf])

    return results


def main():
    global _stop
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT,  _sig)

    ap = argparse.ArgumentParser()
    ap.add_argument("--size",  type=int, default=320, choices=[320, 640])
    ap.add_argument("--model", type=str, required=True)
    args = ap.parse_args()

    size  = args.size
    cap_w = size
    cap_h = size * 3 // 4   # 320→240, 640→480

    err(f"starting: size={size}, cap={cap_w}x{cap_h}, model={args.model}")
    emit({"status": "starting", "size": size})

    # Load ONNX model
    try:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads       = 2   # cores 2-3 only (set via taskset externally)
        opts.inter_op_num_threads       = 1
        opts.enable_mem_pattern         = False  # reduce fragmentation
        opts.enable_cpu_mem_arena       = False
        opts.graph_optimization_level   = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess     = ort.InferenceSession(args.model,
                                        sess_options=opts,
                                        providers=["CPUExecutionProvider"])
        inp_name = sess.get_inputs()[0].name
        err(f"model loaded: input={inp_name}")
        emit({"status": "model_loaded"})
    except Exception as e:
        emit({"status": "error", "msg": f"model load failed: {e}"})
        sys.exit(1)

    frame_times = []

    while not _stop:
        # Camera open loop — retry every 5s if unavailable
        cap = None
        while not _stop:
            try:
                c = cv2.VideoCapture("/dev/video0", cv2.CAP_V4L2)
                if not c.isOpened():
                    raise RuntimeError("/dev/video0 not available")
                c.set(cv2.CAP_PROP_FRAME_WIDTH,  cap_w)
                c.set(cv2.CAP_PROP_FRAME_HEIGHT, cap_h)
                c.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # 1-frame buffer = minimum latency
                c.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                actual_w = int(c.get(cv2.CAP_PROP_FRAME_WIDTH))
                actual_h = int(c.get(cv2.CAP_PROP_FRAME_HEIGHT))
                cap = c
                err(f"camera open: requested {cap_w}x{cap_h}, got {actual_w}x{actual_h}")
                emit({"status": "camera_open", "cap_w": actual_w, "cap_h": actual_h})
                break
            except Exception as e:
                emit({"status": "waiting", "msg": str(e)})
                err(f"camera unavailable: {e} — retry in 5s")
                time.sleep(5)

        if _stop:
            break

        try:
            while not _stop:
                t0  = time.perf_counter()
                ret, frame = cap.read()
                if not ret or frame is None:
                    emit({"status": "waiting", "msg": "frame read failed"})
                    time.sleep(0.05)
                    continue

                h_cap, w_cap = frame.shape[:2]

                # Preprocess
                padded, scale, pad_x, pad_y = letterbox(frame, size)
                inp = padded[:, :, ::-1].astype(np.float32) / 255.0  # BGR→RGB, 0-1
                inp = inp.transpose(2, 0, 1)[np.newaxis]              # HWC→NCHW

                # Inference
                raw_out = sess.run(None, {inp_name: inp})

                # Decode
                dets = decode(raw_out, size, scale, pad_x, pad_y)

                # FPS tracking (rolling average over last 30 frames)
                t1 = time.perf_counter()
                frame_times.append(t1 - t0)
                if len(frame_times) > 30:
                    frame_times.pop(0)
                fps = 1.0 / (sum(frame_times) / len(frame_times)) if frame_times else 0.0

                emit({
                    "dets": dets,
                    "fps":  round(fps, 1),
                    "ts":   t1,
                    "w":    w_cap,
                    "h":    h_cap,
                })

        except Exception as e:
            err(f"inference loop error: {e}")
            emit({"status": "waiting", "msg": str(e)})
        finally:
            if cap:
                cap.release()
                cap = None

        if not _stop:
            err("camera loop restarting in 2s...")
            time.sleep(2)

    err("worker exiting cleanly")


if __name__ == "__main__":
    main()
