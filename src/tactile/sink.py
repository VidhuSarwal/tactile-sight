from __future__ import annotations
from abc import ABC, abstractmethod
import sys
import numpy as np


class Sink(ABC):
    """Hardware seam: real motor drivers implement this interface."""

    @abstractmethod
    def write(self, grid: np.ndarray, frame: bytes) -> None: ...

    @abstractmethod
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# SimDisplay — terminal renderer, no dependencies beyond stdlib + numpy
# ---------------------------------------------------------------------------

# 5-level block character density (low → high intensity)
_CHARS = [' ', '░', '▒', '▓', '█']

# Intensity boundaries separating the 5 levels (upper-exclusive)
_THRESHOLDS = [51, 102, 153, 204]


def _level_to_char(level: int) -> str:
    for i, t in enumerate(_THRESHOLDS):
        if level < t:
            return _CHARS[i]
    return _CHARS[4]


class SimDisplay(Sink):
    """
    Renders the 8×2 haptic grid in the terminal at ~30 Hz.
    Prints raw 16-byte hex frame every hex_interval_frames calls.
    """

    def __init__(self, hex_interval_frames: int = 30) -> None:
        self._hex_interval = hex_interval_frames
        self._call_count = 0
        self._initialized = False

    def write(self, grid: np.ndarray, frame: bytes) -> None:
        out = sys.stdout

        if not self._initialized:
            out.write("\033[2J")   # clear screen once on first frame
            self._initialized = True

        out.write("\033[H")  # cursor home (top-left, no clear = no flicker)

        # Header
        out.write("TactileSight — haptic grid  (col 0 = left, row 0 = top)\n")
        out.write("─" * 40 + "\n")

        # 2 rows, 8 cols — iterate row-first for visual layout
        cols = 8
        rows = 2
        for row in range(rows):
            cells = []
            for col in range(cols):
                lv = frame[col * 2 + row]
                ch = _level_to_char(lv)
                cells.append(f"[{ch}{lv:3d}]")
            out.write("  ".join(cells) + "\n")

        out.write("─" * 40 + "\n")

        # Hex dump once per interval
        self._call_count += 1
        if self._call_count % self._hex_interval == 0:
            hex_str = " ".join(f"{b:02x}" for b in frame)
            out.write(f"FRAME: {hex_str}\n")
        else:
            out.write(" " * 50 + "\n")  # blank line keeps cursor stable

        out.flush()

    def close(self) -> None:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()
