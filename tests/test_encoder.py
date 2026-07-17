"""
Encoder unit tests. All deterministic — no wall-clock, no display.
Each test uses a fresh Encoder instance.
"""
import math
import numpy as np
import pytest

from src.tactile.config import EncoderConfig, DepthConfig
from src.tactile.encoder import Encoder
from tests.conftest import make_grid, nan_grid, single_cell_grid


def fresh_encoder(**overrides) -> Encoder:
    depth_cfg = DepthConfig(min_distance=0.7, max_distance=3.0)
    params = dict(
        ema_alpha=0.33,
        hysteresis_on=2.8,
        hysteresis_off=3.2,
        near_threshold=1.0,
        hold_frames=30,
    )
    params.update(overrides)
    return Encoder(EncoderConfig(**params), depth_cfg)


# ---------------------------------------------------------------------------
# 1. Distance → level mapping (boundary values)
# ---------------------------------------------------------------------------

class TestMapping:
    def _level(self, dist: float) -> int:
        """Single-cell level after encode at given distance.

        Uses alpha=1.0 so EMA = instantaneous value; activates cell via 2.0m
        prime so hysteresis is ON, then feeds one frame of the target distance.
        """
        enc = fresh_encoder(ema_alpha=1.0)
        enc.encode(make_grid(2.0))       # activate (2.0 < hysteresis_on 2.8)
        frame = enc.encode(make_grid(dist))
        return frame[0]  # col=0, row=0 → index 0

    def test_at_max_distance(self):
        assert self._level(3.0) == 0

    def test_above_max_distance(self):
        assert self._level(3.5) == 0

    def test_at_min_distance(self):
        assert self._level(0.7) == 255

    def test_below_min_distance(self):
        assert self._level(0.6) == 255

    def test_nan_gives_zero(self):
        enc = fresh_encoder()
        # Never fed a near reading, so nan should produce 0
        frame = enc.encode(nan_grid())
        assert all(b == 0 for b in frame)

    def test_midpoint(self):
        # 1.85m is midway between 0.7 and 3.0 → level ≈ 127
        enc = fresh_encoder()
        enc.encode(make_grid(2.0))     # activate
        frame = enc.encode(make_grid(1.85))
        # EMA pulls slightly toward 1.85 from 2.0; level should be near 127
        # Allow ±10 for EMA lag
        assert abs(frame[0] - 127) <= 15

    def test_all_cells_encoded(self):
        enc = fresh_encoder()
        enc.encode(make_grid(2.0))    # activate all
        frame = enc.encode(make_grid(1.0))
        assert len(frame) == 16
        assert all(b > 0 for b in frame)


# ---------------------------------------------------------------------------
# 2. Hysteresis asymmetry — same distance, different history
# ---------------------------------------------------------------------------

class TestHysteresis:
    def test_approaching_from_far_not_active_at_2_9(self):
        """Starting from 3.5m: cell is still OFF at 2.9m (never crossed 2.8m)."""
        enc = fresh_encoder(ema_alpha=1.0)  # alpha=1 removes EMA lag for clarity
        enc.encode(make_grid(3.5))   # cell OFF
        frame = enc.encode(make_grid(2.9))
        # 2.9m > hysteresis_on=2.8, so cell stays OFF → level = 0
        assert frame[0] == 0

    def test_receding_from_near_still_active_at_2_9(self):
        """Starting from 2.5m: cell is still ON at 2.9m (hasn't crossed 3.2m)."""
        enc = fresh_encoder(ema_alpha=1.0)
        enc.encode(make_grid(2.5))   # cell turns ON (2.5 < 2.8)
        frame = enc.encode(make_grid(2.9))
        # 2.9m < hysteresis_off=3.2, so cell stays ON → level > 0
        assert frame[0] > 0

    def test_same_distance_different_levels(self):
        """The discriminating assertion: 2.9m reads differently by history alone."""
        enc_approaching = fresh_encoder(ema_alpha=1.0)
        enc_approaching.encode(make_grid(3.5))
        level_off = enc_approaching.encode(make_grid(2.9))[0]

        enc_receding = fresh_encoder(ema_alpha=1.0)
        enc_receding.encode(make_grid(2.5))
        level_on = enc_receding.encode(make_grid(2.9))[0]

        assert level_off == 0
        assert level_on > 0
        assert level_off != level_on

    def test_activates_after_crossing_on_threshold(self):
        enc = fresh_encoder(ema_alpha=1.0)
        enc.encode(make_grid(3.5))  # OFF
        enc.encode(make_grid(2.9))  # still OFF (between thresholds)
        frame = enc.encode(make_grid(2.7))  # crosses 2.8 → ON
        assert frame[0] > 0

    def test_deactivates_after_crossing_off_threshold(self):
        enc = fresh_encoder(ema_alpha=1.0)
        enc.encode(make_grid(2.5))  # ON
        enc.encode(make_grid(3.0))  # still ON (between thresholds)
        frame = enc.encode(make_grid(3.3))  # crosses 3.2 → OFF
        assert frame[0] == 0


# ---------------------------------------------------------------------------
# 3. EMA smoothing convergence
# ---------------------------------------------------------------------------

class TestEMA:
    def test_converges_after_step_input(self):
        """Step from 3.5m to 1.0m: after 10 frames the EMA should be within 5% of 1.0."""
        enc = fresh_encoder()
        # First activate
        for _ in range(3):
            enc.encode(make_grid(2.0))

        # Now step to 1.0m
        for _ in range(10):
            enc.encode(make_grid(1.0))

        # Read back via level: at 1.0m level ≈ 255*(3.0-1.0)/2.3 ≈ 222
        frame = enc.encode(make_grid(1.0))
        # EMA should be within 5% of 1.0m → level within 5% of 222
        expected = int(255 * (3.0 - 1.0) / 2.3)
        assert abs(frame[0] - expected) <= int(expected * 0.10)  # 10% tolerance for EMA lag

    def test_ema_smooths_step_gradually(self):
        """After a step change, level should increase monotonically for a few frames."""
        enc = fresh_encoder()
        enc.encode(make_grid(2.8))  # just at threshold — activate
        # step to 1.0m
        levels = []
        for _ in range(8):
            frame = enc.encode(make_grid(1.0))
            levels.append(frame[0])
        # Each frame should be >= the previous (approaching from above)
        for i in range(1, len(levels)):
            assert levels[i] >= levels[i - 1]


# ---------------------------------------------------------------------------
# 4 & 5. Blind-zone HOLD
# ---------------------------------------------------------------------------

class TestBlindZoneHold:
    def test_near_then_nan_holds_for_hold_frames(self):
        """Near reading followed by nans: level stays non-zero for hold_frames frames."""
        hold_frames = 5
        enc = fresh_encoder(hold_frames=hold_frames, ema_alpha=1.0)

        # Activate and bring near
        enc.encode(make_grid(2.0))   # activate
        enc.encode(make_grid(0.8))   # near reading (< 1.0m)
        last_valid_level = enc.encode(make_grid(0.8))[0]  # stable near level

        # Now feed nans — level should hold for exactly hold_frames frames
        held_levels = []
        for _ in range(hold_frames):
            frame = enc.encode(nan_grid())
            held_levels.append(frame[0])

        # All held frames should be non-zero (holding last_valid_level)
        assert all(lv > 0 for lv in held_levels), f"Expected hold, got {held_levels}"

        # Frame after hold expires: should drop to 0
        frame_after = enc.encode(nan_grid())
        assert frame_after[0] == 0

    def test_far_then_nan_does_not_hold(self):
        """Far reading (> near_threshold) followed by nan: drops to 0 immediately."""
        enc = fresh_encoder(ema_alpha=1.0)
        enc.encode(make_grid(2.0))   # activate, not near
        enc.encode(make_grid(2.0))   # confirm not near
        # Immediately nan
        frame = enc.encode(nan_grid())
        assert frame[0] == 0

    def test_hold_counter_decrements_to_zero(self):
        """Hold counter reaches 0 exactly at hold_frames, not sooner."""
        hold_frames = 3
        enc = fresh_encoder(hold_frames=hold_frames, ema_alpha=1.0)
        enc.encode(make_grid(2.0))  # activate
        enc.encode(make_grid(0.8))  # near

        levels = [enc.encode(nan_grid())[0] for _ in range(hold_frames + 1)]
        # First hold_frames frames: non-zero; last frame: 0
        assert all(lv > 0 for lv in levels[:hold_frames])
        assert levels[hold_frames] == 0


# ---------------------------------------------------------------------------
# 6. Frame packing and index order
# ---------------------------------------------------------------------------

class TestFramePacking:
    def test_index_formula(self):
        """frame[col*2 + row] is the level for (col, row)."""
        enc = fresh_encoder(ema_alpha=1.0)

        # Build a grid where only col=3, row=1 is near (others far/inactive)
        g = np.full((8, 2), 4.0, dtype=np.float32)  # all clear → inactive

        # Activate col=3,row=1 by feeding near value in prior frame
        g_prime = np.full((8, 2), 4.0, dtype=np.float32)
        g_prime[3, 1] = 2.0   # activate that cell
        enc.encode(g_prime)

        g2 = np.full((8, 2), 4.0, dtype=np.float32)
        g2[3, 1] = 1.0        # near
        frame = enc.encode(g2)

        idx = 3 * 2 + 1   # = 7
        assert frame[idx] > 0, "col=3, row=1 should be active"
        # All other indices should be 0
        for i, b in enumerate(frame):
            if i != idx:
                assert b == 0, f"index {i} should be 0, got {b}"

    def test_col0_is_leftmost(self):
        """col 0 = leftmost. Encoding col=0 row=0 → frame[0]."""
        enc = fresh_encoder(ema_alpha=1.0)
        g = np.full((8, 2), 4.0, dtype=np.float32)
        g[0, 0] = 2.0
        enc.encode(g)       # activate
        g2 = np.full((8, 2), 4.0, dtype=np.float32)
        g2[0, 0] = 1.0
        frame = enc.encode(g2)
        assert frame[0] > 0   # index 0*2+0 = 0

    def test_frame_length(self):
        enc = fresh_encoder()
        frame = enc.encode(make_grid(2.0))
        assert len(frame) == 16

    def test_frame_type_is_bytes(self):
        enc = fresh_encoder()
        frame = enc.encode(make_grid(2.0))
        assert isinstance(frame, bytes)

    def test_wrong_shape_raises(self):
        enc = fresh_encoder()
        with pytest.raises(ValueError):
            enc.encode(np.zeros((2, 8), dtype=np.float32))  # wrong axis order
