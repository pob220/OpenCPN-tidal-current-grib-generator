from pathlib import Path

import pytest

from tidal_current_grib_generator.reference import compare_reference_csv
from tidal_current_grib_generator.sources.synthetic import ConstantCurrentSource


def test_reference_comparison_with_constant_source(tmp_path: Path):
    reference = tmp_path / "reference.csv"
    output = tmp_path / "comparison.csv"
    reference.write_text(
        "name,lat,lon,time_utc,reference_speed_knots,reference_direction_degrees,source_note\n"
        "point,52.0,-5.0,2026-07-01T00:00:00Z,1.943846,90.0,synthetic fixture\n"
    )
    rows = compare_reference_csv(ConstantCurrentSource(u=1.0, v=0.0), reference, output)
    assert len(rows) == 1
    assert rows[0].speed_error_knots == pytest.approx(0.0, abs=1e-6)
    assert rows[0].direction_error_degrees == pytest.approx(0.0)
    assert output.exists()
