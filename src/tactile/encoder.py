from __future__ import annotations
import math
from typing import Optional
import numpy as np

from .config import EncoderConfig, DepthConfig

_COLS = 8
_ROWS = 2
_N = _COLS * _ROWS  # 16 cells


class _CellState:
    __slots__ = ("ema_val", "is_active", "hold_counter", "last_level", "last_near")

    def __init__(self) -> None:
        self.ema_val: Optional[float] = None  # None until first valid sample
        self.is_active: bool = False
        self.hold_counter: int = 0
        self.last_level: int = 0
        self.last_near: bool = False


class Encoder:
    """
    Maps an (8,2) distance grid to a 16-byte haptic-level frame.

    Fully deterministic — no wall-clock. Call encode() once per logical frame.
    Index layout: frame[col*2 + row], col 0 = wearer's left, row 0 = top.
    """

    def __init__(self, enc_cfg: EncoderConfig, depth_cfg: DepthConfig) -> None:
        self._alpha = enc_cfg.ema_alpha
        self._hyst_on = enc_cfg.hysteresis_on
        self._hyst_off = enc_cfg.hysteresis_off
        self._near_thr = enc_cfg.near_threshold
        self._hold_frames = enc_cfg.hold_frames
        self._d_min = depth_cfg.min_distance
        self._d_max = depth_cfg.max_distance
        self._d_range = depth_cfg.max_distance - depth_cfg.min_distance
        self._cells: list[_CellState] = [_CellState() for _ in range(_N)]

    def encode(self, grid: np.ndarray) -> bytes:
        """Accept (8,2) float32 array; return 16-byte level frame."""
        if grid.shape != (_COLS, _ROWS):
            raise ValueError(f"Expected grid shape ({_COLS},{_ROWS}), got {grid.shape}")

        frame = bytearray(_N)
        for col in range(_COLS):
            for row in range(_ROWS):
                idx = col * 2 + row
                cell = self._cells[idx]
                dist = float(grid[col, row])
                frame[idx] = self._process(cell, dist)
        return bytes(frame)

    def _process(self, cell: _CellState, dist: float) -> int:
        if not math.isnan(dist):
            # EMA update
            if cell.ema_val is None:
                cell.ema_val = dist
            else:
                cell.ema_val = self._alpha * dist + (1.0 - self._alpha) * cell.ema_val

            cell.last_near = dist < self._near_thr
            cell.hold_counter = 0  # clear any hold — we have a valid reading

            # Hysteresis state machine
            if cell.is_active and cell.ema_val > self._hyst_off:
                cell.is_active = False
            elif not cell.is_active and cell.ema_val < self._hyst_on:
                cell.is_active = True

            # Level from smoothed distance
            if not cell.is_active:
                level = 0
            elif cell.ema_val >= self._d_max:
                level = 0
            elif cell.ema_val <= self._d_min:
                level = 255
            else:
                level = int(255 * (self._d_max - cell.ema_val) / self._d_range)

            cell.last_level = level
            return level

        else:  # nan
            if cell.last_near and cell.hold_counter == 0:
                cell.hold_counter = self._hold_frames  # arm hold

            if cell.hold_counter > 0:
                cell.hold_counter -= 1
                if cell.hold_counter == 0:
                    cell.last_near = False  # prevent re-arming after hold expires
                return cell.last_level
            else:
                return 0
