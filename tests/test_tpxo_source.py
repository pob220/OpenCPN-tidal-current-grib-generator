from pathlib import Path
import types

import numpy as np
import pytest

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, parse_utc_datetime
from tidal_current_grib_generator.sources import create_source
from tidal_current_grib_generator.sources.pytmd import (
    _component_values,
    _component_time_values,
    inspect_pytmd_source,
    pytmd_is_available,
)


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


def test_tpxo_passes_flattened_meshgrid_to_pytmd(monkeypatch, tmp_path: Path):
    calls = {}

    class FakeCompute:
        @staticmethod
        def tide_currents(x, y, delta_time, **kwargs):
            calls["x"] = x
            calls["y"] = y
            calls["delta_time"] = delta_time
            calls["kwargs"] = kwargs
            return {"u": np.ones(x.shape), "v": np.ones(x.shape) * 2.0}

    monkeypatch.setattr("tidal_current_grib_generator.sources.pytmd._import_pytmd_compute", lambda: FakeCompute)
    source = create_source("tpxo", model_directory=tmp_path)
    bbox = BoundingBox(-6.0, 53.0, -5.5, 53.5)
    grid = build_regular_grid(bbox, 0.25)
    current = source.get_current_grid(bbox, parse_utc_datetime("2026-07-04T00:00:00Z"), grid)
    assert calls["x"].shape == (grid.nx * grid.ny,)
    assert calls["y"].shape == (grid.nx * grid.ny,)
    assert calls["delta_time"].shape == (grid.nx * grid.ny,)
    assert calls["kwargs"]["type"] == "drift"
    assert calls["kwargs"]["chunks"] == "auto"
    assert calls["kwargs"]["buffer"] == 1.0
    assert current.u_mps.shape == grid.shape
    assert np.allclose(current.u_mps, 0.01)
    assert np.allclose(current.v_mps, 0.02)


def test_tpxo_component_values_reshapes_flattened_point_output():
    values = np.arange(6, dtype=float)
    result = _component_values({"u": values}, "u", (2, 3))
    assert result.shape == (2, 3)
    assert np.array_equal(result, values.reshape(2, 3))


def test_tpxo_component_values_reshapes_batched_flattened_point_output():
    values = np.arange(12, dtype=float)
    result = _component_time_values({"u": values}, "u", (2, 2, 3))
    assert result.shape == (2, 2, 3)
    assert np.array_equal(result, values.reshape(2, 2, 3))


def test_tpxo_component_values_reshapes_transposed_output():
    values = np.arange(6, dtype=float).reshape(3, 2)
    result = _component_values({"u": values}, "u", (2, 3))
    assert result.shape == (2, 3)
    assert np.array_equal(result, values.T)


def test_tpxo_component_values_handles_masked_arrays():
    values = np.ma.array([[1.0, 2.0], [3.0, 4.0]], mask=[[False, True], [False, False]])
    result = _component_values({"u": values}, "u", (2, 2))
    assert np.isnan(result[0, 1])
    assert result[1, 0] == 3.0


def test_tpxo_component_values_rejects_bad_shape():
    with pytest.raises(ValidationError, match="cannot safely reshape"):
        _component_values({"u": np.ones((5,))}, "u", (2, 3))


def test_inspect_tpxo_filename_fallback_discovers_constituents(monkeypatch, tmp_path: Path):
    for constituent in ("2n2", "k1", "m2", "s2"):
        (tmp_path / f"u_{constituent}_tpxo10atlas_v2.nc").write_text("")
        (tmp_path / f"h_{constituent}_tpxo10atlas_v2.nc").write_text("")
    (tmp_path / "grid_tpxo10atlas_v2.nc").write_text("")

    class FakeModel:
        format = "ATLAS-netcdf"
        projection = "WGS84 longlat"
        u = object()
        v = object()

        def parse_constituents(self, group=None):
            raise TypeError("attribute name must be string, not 'int'")

    class FakeFactory:
        def __init__(self, directory, verify=False):
            pass

        def from_database(self, model_name, group=None):
            return FakeModel()

    fake_pyTMD = types.SimpleNamespace(io=types.SimpleNamespace(model=FakeFactory))
    monkeypatch.setitem(__import__("sys").modules, "pyTMD", fake_pyTMD)
    monkeypatch.setitem(__import__("sys").modules, "pyTMD.io", fake_pyTMD.io)
    monkeypatch.setattr("tidal_current_grib_generator.sources.pytmd.pytmd_is_available", lambda: True)

    inspection = inspect_pytmd_source(tmp_path, "TPXO10-atlas-v2-nc").as_dict()
    assert inspection["current_prediction_available"] is True
    assert inspection["constituents_u"] == ["2n2", "k1", "m2", "s2"]
    assert inspection["constituents_v"] == ["2n2", "k1", "m2", "s2"]
    assert any("u constituents discovered from filenames" in detail for detail in inspection["details"])
