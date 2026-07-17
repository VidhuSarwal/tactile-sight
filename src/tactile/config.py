from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import yaml


@dataclass
class DepthConfig:
    min_distance: float
    max_distance: float


@dataclass
class EncoderConfig:
    ema_alpha: float
    hysteresis_on: float
    hysteresis_off: float
    near_threshold: float
    hold_frames: int


@dataclass
class DisplayConfig:
    frame_rate: int
    hex_interval_frames: int


@dataclass
class Config:
    depth: DepthConfig
    encoder: EncoderConfig
    display: DisplayConfig


def load(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text())
    d = raw["depth"]
    e = raw["encoder"]
    disp = raw["display"]
    return Config(
        depth=DepthConfig(
            min_distance=float(d["min_distance"]),
            max_distance=float(d["max_distance"]),
        ),
        encoder=EncoderConfig(
            ema_alpha=float(e["ema_alpha"]),
            hysteresis_on=float(e["hysteresis_on"]),
            hysteresis_off=float(e["hysteresis_off"]),
            near_threshold=float(e["near_threshold"]),
            hold_frames=int(e["hold_frames"]),
        ),
        display=DisplayConfig(
            frame_rate=int(disp["frame_rate"]),
            hex_interval_frames=int(disp["hex_interval_frames"]),
        ),
    )
