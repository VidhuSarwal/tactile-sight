from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Generator, Optional
import math
import numpy as np


class DepthSource(ABC):
    """Hardware seam: real cameras implement this interface."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def get_grid(self) -> Optional[np.ndarray]:
        """Return (8, 2) float32 array in meters; np.nan = invalid cell.
        axis-0 = col (0 = wearer's left), axis-1 = row (0 = top)."""
        ...

    @abstractmethod
    def stop(self) -> None: ...


# ---------------------------------------------------------------------------
# Noise helpers applied on top of every scene
# ---------------------------------------------------------------------------

_RNG = np.random.default_rng()
_NOISE_STD = 0.02   # per-cell Gaussian jitter (meters)
_NAN_PROB = 0.03    # probability any single cell is invalid


def _apply_noise(grid: np.ndarray) -> np.ndarray:
    out = grid + _RNG.normal(0.0, _NOISE_STD, grid.shape).astype(np.float32)
    nan_mask = _RNG.random(grid.shape) < _NAN_PROB
    out[nan_mask] = np.nan
    return out


# ---------------------------------------------------------------------------
# Scene generators — yield clean (8,2) float32 base frames indefinitely
# ---------------------------------------------------------------------------

def _wall_approach() -> Generator[np.ndarray, None, None]:
    """Uniform field closing from 3.0 m to 0.5 m over 150 frames, then loop."""
    n = 150
    while True:
        for i in range(n):
            d = 3.0 - (i / (n - 1)) * 2.5
            yield np.full((8, 2), d, dtype=np.float32)


def _doorway_left() -> Generator[np.ndarray, None, None]:
    """Cols 0-2 open (4.0 m); cols 3-7 have a wall that pulses 1.5→0.8→1.5 m."""
    n = 90
    while True:
        for i in range(n):
            # sinusoidal pulse so the wall "breathes" (simulates approach/retreat)
            t = math.pi * i / (n - 1)
            d_wall = 1.5 - 0.7 * math.sin(t)
            grid = np.full((8, 2), d_wall, dtype=np.float32)
            grid[0:3, :] = 4.0   # left corridor is open
            yield grid


def _person_crossing() -> Generator[np.ndarray, None, None]:
    """Near blob (0.9 m) at Gaussian width ~1.5 cols, marching col 7→0 over 80 frames."""
    n = 80
    cols = np.arange(8, dtype=np.float32)
    sigma = 1.5
    far = 3.5
    near = 0.9
    while True:
        for i in range(n):
            center = 7.0 - 7.0 * i / (n - 1)
            weights = np.exp(-0.5 * ((cols - center) / sigma) ** 2)  # shape (8,)
            # blend: high weight → near distance
            col_dist = far - (far - near) * weights          # shape (8,)
            grid = np.stack([col_dist, col_dist], axis=1)    # shape (8,2)
            yield grid.astype(np.float32)


def _all_clear() -> Generator[np.ndarray, None, None]:
    while True:
        yield np.full((8, 2), 4.0, dtype=np.float32)


_SCENES: dict[str, type[Generator]] = {
    "wall_approach": _wall_approach,
    "doorway_left": _doorway_left,
    "person_crossing": _person_crossing,
    "all_clear": _all_clear,
}


# ---------------------------------------------------------------------------
# MockSource
# ---------------------------------------------------------------------------

class MockSource(DepthSource):
    """Scripted scene source. Optionally keyboard-steppable."""

    def __init__(self, scene: str = "wall_approach", step: bool = False) -> None:
        if scene not in _SCENES:
            raise ValueError(f"Unknown scene '{scene}'. Choose from: {list(_SCENES)}")
        self._scene_name = scene
        self._step = step
        self._gen: Optional[Generator] = None

    def start(self) -> None:
        self._gen = _SCENES[self._scene_name]()

    def get_grid(self) -> Optional[np.ndarray]:
        if self._gen is None:
            raise RuntimeError("Call start() before get_grid()")
        if self._step:
            input("  [step] press Enter for next frame…")
        base = next(self._gen)
        return _apply_noise(base)

    def stop(self) -> None:
        self._gen = None


AVAILABLE_SCENES: list[str] = list(_SCENES)
