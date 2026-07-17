"""
MockSource unit tests. No display, no sleep.
"""
import numpy as np
import pytest

from src.tactile.depth_source import MockSource, AVAILABLE_SCENES


@pytest.mark.parametrize("scene", AVAILABLE_SCENES)
def test_scene_returns_correct_shape(scene: str) -> None:
    src = MockSource(scene=scene)
    src.start()
    grid = src.get_grid()
    src.stop()
    assert grid is not None
    assert grid.shape == (8, 2)
    assert grid.dtype == np.float32


@pytest.mark.parametrize("scene", AVAILABLE_SCENES)
def test_scene_does_not_raise_over_many_frames(scene: str) -> None:
    src = MockSource(scene=scene)
    src.start()
    for _ in range(200):
        g = src.get_grid()
        assert g is not None
    src.stop()


def test_all_clear_cells_are_far() -> None:
    """all_clear: valid cells should all be well beyond the 3.0m threshold."""
    src = MockSource(scene="all_clear")
    src.start()
    for _ in range(10):
        g = src.get_grid()
        valid = g[~np.isnan(g)]
        assert np.all(valid >= 3.5), f"Expected all valid cells >= 3.5m, got min={valid.min():.2f}"
    src.stop()


def test_wall_approach_decreases() -> None:
    """wall_approach: mean valid distance should decrease over first 10 frames."""
    src = MockSource(scene="wall_approach")
    src.start()
    means = []
    for _ in range(10):
        g = src.get_grid()
        valid = g[~np.isnan(g)]
        if len(valid) > 0:
            means.append(valid.mean())
    src.stop()
    # At least 5 frames with valid readings, and later frames are closer
    assert len(means) >= 5
    # Average of first half should be greater than average of second half
    half = len(means) // 2
    assert np.mean(means[:half]) > np.mean(means[half:])


def test_person_crossing_has_near_blob() -> None:
    """person_crossing: at least one cell should be near (< 1.5m) in early frames."""
    src = MockSource(scene="person_crossing")
    src.start()
    any_near = False
    for _ in range(20):
        g = src.get_grid()
        valid = g[~np.isnan(g)]
        if np.any(valid < 1.5):
            any_near = True
            break
    src.stop()
    assert any_near, "Expected a near blob in person_crossing scene"


def test_get_grid_without_start_raises() -> None:
    src = MockSource(scene="wall_approach")
    with pytest.raises(RuntimeError):
        src.get_grid()


def test_unknown_scene_raises() -> None:
    with pytest.raises(ValueError):
        MockSource(scene="nonexistent_scene")


def test_stop_and_restart() -> None:
    """Starting, stopping, and restarting should work cleanly."""
    src = MockSource(scene="all_clear")
    src.start()
    g1 = src.get_grid()
    src.stop()
    src.start()
    g2 = src.get_grid()
    src.stop()
    assert g1 is not None and g2 is not None
