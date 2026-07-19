#!/usr/bin/env python3
"""Reads raw depth frames from OpenNI2 and writes them to stdout.

Designed to run as a subprocess of haptic_depth_server.py so that a SIGSEGV
inside libOpenNI2.so (which happens after ~2300 frames on this hardware) only
kills this process — the HTTP/WebSocket server stays alive.

Frame format on stdout:
  [uint16 w][uint16 h][uint32 data_len][data_len bytes of uint16 depth data]
= 8-byte header + 614400 bytes per frame at 640×480
"""
import sys, os, ctypes, struct, time

NIDIR = os.environ.get('LD_LIBRARY_PATH', os.getcwd())

log = lambda m: print(f"[cam] {m}", file=sys.stderr, flush=True)

MAX_FRAME = 640 * 480 * 2   # 614400 bytes

def main():
    # Allow camera USB to settle after parent process restart
    time.sleep(3)

    lib_path = os.path.join(NIDIR, 'libOpenNI2.so')
    try:
        lib = ctypes.CDLL(lib_path)
    except OSError as e:
        log(f"cannot load {lib_path}: {e}")
        sys.exit(1)

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

    log("initializing OpenNI2...")
    if lib.oniInitialize(2) != 0:
        log("oniInitialize failed — exiting")
        sys.exit(1)

    while True:
        dev    = ctypes.c_void_p()
        stream = ctypes.c_void_p()
        try:
            if lib.oniDeviceOpen(None, ctypes.byref(dev)) != 0:
                raise RuntimeError("no camera — retrying...")
            if lib.oniDeviceCreateStream(dev, 3, ctypes.byref(stream)) != 0:
                raise RuntimeError("oniDeviceCreateStream(depth=3) failed")
            if lib.oniStreamStart(stream) != 0:
                raise RuntimeError("oniStreamStart failed")

            log("stream up — warming up 5 frames...")
            for _ in range(5):
                frame = ctypes.c_void_p()
                lib.oniStreamReadFrame(stream, ctypes.byref(frame))
                if frame.value:
                    lib.oniFrameRelease(ctypes.byref(frame))

            log("streaming")
            streams_arr  = (ctypes.c_void_p * 1)(stream.value)
            stream_idx   = ctypes.c_int(-1)
            timeout_streak = 0
            out = sys.stdout.buffer

            while True:
                rc_wait = lib.oniWaitForAnyStream(streams_arr, 1,
                                                  ctypes.byref(stream_idx), 500)
                if rc_wait != 0:
                    if rc_wait == 102:
                        timeout_streak += 1
                        if timeout_streak < 10:
                            continue
                        raise RuntimeError("stream timed out 10× — USB disconnected?")
                    raise RuntimeError(f"stream lost (rc={rc_wait})")
                timeout_streak = 0

                frame = ctypes.c_void_p()
                rc = lib.oniStreamReadFrame(stream, ctypes.byref(frame))
                if rc != 0 or not frame.value:
                    continue

                hdr       = ctypes.string_at(frame.value, 80)
                data_size = struct.unpack_from('<i', hdr, 0)[0]
                data_addr = struct.unpack_from('<Q', hdr, 8)[0]
                w         = struct.unpack_from('<i', hdr, 36)[0]
                h         = struct.unpack_from('<i', hdr, 40)[0]

                if (data_addr and 0 < data_size <= MAX_FRAME
                        and 0 < w <= 640 and 0 < h <= 480):
                    raw = ctypes.string_at(data_addr, data_size)
                    lib.oniFrameRelease(ctypes.byref(frame))
                    # Write frame to stdout: 8-byte header + frame bytes
                    out.write(struct.pack('<HHI', w, h, len(raw)))
                    out.write(raw)
                    out.flush()
                    del raw
                else:
                    lib.oniFrameRelease(ctypes.byref(frame))

        except Exception as e:
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
        log("retry in 5s...")
        time.sleep(5)


if __name__ == '__main__':
    main()
