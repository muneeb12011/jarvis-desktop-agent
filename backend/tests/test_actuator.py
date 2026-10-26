"""
Tests for the actuator's coordinate rescaling — this is the exact
calculation that determines where a click actually lands on the real
screen, so getting it wrong means every action is silently miscalibrated.
"""

from app.actuator import rescale_point


def test_rescale_identity_when_sizes_match():
    assert rescale_point(100, 200, (1280, 720), (1280, 720)) == (100, 200)


def test_rescale_scales_up_correctly():
    # Model saw a 640x360 image, real screen is 1920x1080 (exactly 3x).
    x, y = rescale_point(100, 100, (640, 360), (1920, 1080))
    assert (x, y) == (300, 300)


def test_rescale_scales_down_correctly():
    x, y = rescale_point(300, 300, (1920, 1080), (640, 360))
    assert (x, y) == (100, 100)


def test_rescale_handles_non_uniform_aspect():
    x, y = rescale_point(50, 50, (100, 200), (200, 200))
    assert x == 100  # x doubled
    assert y == 50  # y unchanged
