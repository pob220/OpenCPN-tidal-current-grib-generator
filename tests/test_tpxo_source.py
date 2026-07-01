from pathlib import Path

import pytest

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, parse_utc_datetime
from tidal_current_grib_generator.sources import create_source
from tidal_current_grib_generator.sources.pytmd import inspect_pytmd_source, pytmd_is_available


def test_tpxo_requires_model_dir():
    with pytest.raises(ValidationError, match="--model-dir"):
        create_source("tpxo")


def test_tpxo_missing_model_dir_errors_before_dependency_check(tmp_path: Path):
    source = create_source("tpxo", model_directory=tmp_path / "missing")
    bbox = BoundingBox(-5.0, 52.0, -4.99, 52.01)
    grid = build_regular_grid(bbox, 0.01)
    with pytest.raises(ValidationError, match="model directory does not exist"):
        source.get_current_grid(bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid)


def test_tpxo_without_pytmd_errors_clearly_if_dependency_missing(tmp_path: Path):
    if pytmd_is_available():
        pytest.skip("pyTMD is installed in this environment")
    source = create_source("tpxo", model_directory=tmp_path)
    bbox = BoundingBox(-5.0, 52.0, -4.99, 52.01)
    grid = build_regular_grid(bbox, 0.01)
    with pytest.raises(MissingDependencyError, match="pyTMD is not installed"):
        source.get_current_grid(bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid)


def test_inspect_tpxo_reports_missing_dependency(tmp_path: Path):
    inspection = inspect_pytmd_source(tmp_path, "TPXO10-atlas-v2-nc").as_dict()
    assert inspection["name"] == "tpxo"
    assert inspection["model_directory_exists"] is True
    if not pytmd_is_available():
        assert inspection["pytmd_available"] is False
        assert inspection["current_prediction_available"] is False
