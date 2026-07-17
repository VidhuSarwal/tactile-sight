#!/usr/bin/env python3
"""TactileSight Band — Increment 0: depth-to-haptic pipeline."""
from __future__ import annotations
import argparse
import time
from pathlib import Path

from src.tactile import config as cfg_mod
from src.tactile.depth_source import MockSource, AVAILABLE_SCENES
from src.tactile.encoder import Encoder
from src.tactile.sink import SimDisplay


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TactileSight haptic pipeline (beta)")
    p.add_argument("--source", choices=["mock"], default="mock")
    p.add_argument("--scene", choices=AVAILABLE_SCENES, default="wall_approach")
    p.add_argument("--sink", choices=["sim"], default="sim")
    p.add_argument("--step", action="store_true", help="keyboard-step mode (Enter per frame)")
    p.add_argument("--config", default="config.yaml", help="path to config.yaml")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    config = cfg_mod.load(Path(args.config))

    source = MockSource(scene=args.scene, step=args.step)
    encoder = Encoder(config.encoder, config.depth)
    sink = SimDisplay(hex_interval_frames=config.display.hex_interval_frames)

    frame_period = 1.0 / config.display.frame_rate

    source.start()
    try:
        while True:
            t0 = time.monotonic()
            grid = source.get_grid()
            frame = encoder.encode(grid)
            sink.write(grid, frame)
            elapsed = time.monotonic() - t0
            remaining = frame_period - elapsed
            if remaining > 0 and not args.step:
                time.sleep(remaining)
    except KeyboardInterrupt:
        pass
    finally:
        source.stop()
        sink.close()


if __name__ == "__main__":
    main()
