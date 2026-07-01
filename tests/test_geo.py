from datetime import timezone

import pytest

from tidal_current_grib_generator.errors import ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, build_time_sequence, parse_utc_datetime


def test_bbox_validation_accepts_normal_bbox():
    bbox = BoundingBox.from_values([-7.0, 51.5, -4.0, 55.5])
    assert bbox.west == -7.0


def test_bbox_validation_rejects_inverted_bbox():
    with pytest.raises(ValidationError):
        BoundingBox.from_values([-4.0, 51.5, -7.0, 55.5])


def test_grid_generation_is_inclusive():
    grid = build_regular_grid(BoundingBox(-1.0, 50.0, 0.0, 51.0), 0.5)
    assert grid.shape == (3, 3)
    assert grid.longitudes.tolist() == [-1.0, -0.5, 0.0]
    assert grid.latitudes.tolist() == [50.0, 50.5, 51.0]


def test_time_sequence_generation():
    start = parse_utc_datetime("2026-07-01T00:00:00Z")
    times = build_time_sequence(start, hours=6, step_hours=3)
    assert len(times) == 3
    assert all(t.tzinfo == timezone.utc for t in times)
    assert times[-1].hour == 6


def test_time_sequence_rejects_non_divisible_range():
    start = parse_utc_datetime("2026-07-01T00:00:00Z")
    with pytest.raises(ValidationError):
        build_time_sequence(start, hours=5, step_hours=2)
