import pytest

from tidal_current_grib_generator.model import (
    components_to_speed_direction,
    direction_error_degrees,
    speed_direction_to_components,
)


def test_speed_direction_conversion_eastward():
    speed, direction = components_to_speed_direction(0.514444, 0.0)
    assert speed == pytest.approx(1.0)
    assert direction == pytest.approx(90.0)


def test_direction_error_wraps_to_signed_range():
    assert direction_error_degrees(350.0, 10.0) == pytest.approx(-20.0)
    assert direction_error_degrees(10.0, 350.0) == pytest.approx(20.0)


def test_speed_direction_to_components_knots():
    u, v = speed_direction_to_components(2.0, 90.0, units="knots")
    assert u == pytest.approx(1.028888)
    assert abs(v) < 1e-12
