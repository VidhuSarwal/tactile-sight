import pytest
import numpy as np
from src.tactile.config import EncoderConfig, DepthConfig


@pytest.fixture
def depth_cfg() -> DepthConfig:
    return DepthConfig(min_distance=0.7, max_distance=3.0)


@pytest.fixture
def enc_cfg() -> EncoderConfig:
    return EncoderConfig(
        ema_alpha=0.33,
        hysteresis_on=2.8,
        hysteresis_off=3.2,
        near_threshold=1.0,
        hold_frames=30,
    )


@pytest.fixture
def encoder(enc_cfg, depth_cfg):
    from src.tactile.encoder import Encoder
    return Encoder(enc_cfg, depth_cfg)


def make_grid(distance: float) -> np.ndarray:
    """Uniform (8,2) grid at a given distance."""
    return np.full((8, 2), distance, dtype=np.float32)


def nan_grid() -> np.ndarray:
    return np.full((8, 2), np.nan, dtype=np.float32)


def single_cell_grid(col: int, row: int, distance: float) -> np.ndarray:
    """(8,2) grid of nan except one cell."""
    g = np.full((8, 2), np.nan, dtype=np.float32)
    g[col, row] = distance
    return g
