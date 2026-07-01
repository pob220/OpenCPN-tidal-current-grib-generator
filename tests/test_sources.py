import numpy as np

from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, parse_utc_datetime
from tidal_current_grib_generator.sources.synthetic import ConstantCurrentSource, SyntheticRotaryTideSource


def test_constant_current_source_values():
    bbox = BoundingBox(-1.0, 50.0, 0.0, 51.0)
    grid = build_regular_grid(bbox, 0.5)
    current = ConstantCurrentSource(u=1.0, v=2.0).get_current_grid(
        bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid
    )
    assert current.u_mps.shape == (3, 3)
    assert (current.u_mps == 1.0).all()
    assert (current.v_mps == 2.0).all()


def test_synthetic_source_is_deterministic():
    bbox = BoundingBox(-7.0, 51.5, -6.5, 52.0)
    grid = build_regular_grid(bbox, 0.25)
    time = parse_utc_datetime("2026-07-01T00:00:00Z")
    source = SyntheticRotaryTideSource()
    a = source.get_current_grid(bbox, time, grid)
    b = source.get_current_grid(bbox, time, grid)
    assert (a.u_mps == b.u_mps).all()
    assert (a.v_mps == b.v_mps).all()
    assert np.ptp(a.u_mps) > 0.0
