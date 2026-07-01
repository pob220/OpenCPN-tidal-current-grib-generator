from pathlib import Path

import numpy as np
import pytest

from tidal_current_grib_generator.cli import main
from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, parse_utc_datetime
from tidal_current_grib_generator.sources import create_source
from tidal_current_grib_generator.sources.netcdf import inspect_netcdf, xarray_is_available


def _write_fixture(path: Path, units: str = "m/s", include_v: bool = True) -> None:
    xr = pytest.importorskip("xarray")
    times = np.array(["2026-07-01T00:00:00", "2026-07-01T01:00:00"], dtype="datetime64[ns]")
    latitudes = np.array([51.5, 52.0, 52.5])
    longitudes = np.array([-7.0, -6.5, -6.0])
    u = np.ones((2, 3, 3), dtype=float)
    v = np.full((2, 3, 3), 2.0, dtype=float)
    data_vars = {
        "eastward_sea_water_velocity": (
            ("time", "latitude", "longitude"),
            u,
            {"units": units},
        ),
    }
    if include_v:
        data_vars["northward_sea_water_velocity"] = (
            ("time", "latitude", "longitude"),
            v,
            {"units": units},
        )
    xr.Dataset(
        data_vars=data_vars,
        coords={"time": times, "latitude": latitudes, "longitude": longitudes},
    ).to_netcdf(path)


def test_netcdf_missing_dependency_or_missing_file(tmp_path: Path):
    if xarray_is_available():
        pytest.skip("xarray is installed in this environment")
    source = create_source("netcdf", input_netcdf=tmp_path / "missing.nc")
    bbox = BoundingBox(-7.0, 51.5, -6.0, 52.5)
    grid = build_regular_grid(bbox, 0.5)
    with pytest.raises(ValidationError, match="does not exist"):
        source.get_current_grid(bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid)
    existing = tmp_path / "existing.nc"
    existing.write_bytes(b"")
    with pytest.raises(MissingDependencyError):
        inspect_netcdf(existing)


def test_inspect_netcdf_fixture(tmp_path: Path):
    pytest.importorskip("xarray")
    path = tmp_path / "currents.nc"
    _write_fixture(path)
    inspection = inspect_netcdf(path)
    assert inspection["detected_u_variable"] == "eastward_sea_water_velocity"
    assert inspection["detected_v_variable"] == "northward_sea_water_velocity"
    assert inspection["latitude_range"] == (51.5, 52.5)


def test_netcdf_source_current_grid_mps(tmp_path: Path):
    pytest.importorskip("xarray")
    path = tmp_path / "currents.nc"
    _write_fixture(path, units="m/s")
    source = create_source("netcdf", input_netcdf=path)
    bbox = BoundingBox(-7.0, 51.5, -6.0, 52.5)
    grid = build_regular_grid(bbox, 0.5)
    current = source.get_current_grid(bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid)
    assert current.u_mps.shape == (3, 3)
    assert np.allclose(current.u_mps, 1.0)
    assert np.allclose(current.v_mps, 2.0)


def test_netcdf_source_current_grid_cmps(tmp_path: Path):
    pytest.importorskip("xarray")
    path = tmp_path / "currents.nc"
    _write_fixture(path, units="cm/s")
    source = create_source("netcdf", input_netcdf=path)
    bbox = BoundingBox(-7.0, 51.5, -6.0, 52.5)
    grid = build_regular_grid(bbox, 0.5)
    current = source.get_current_grid(bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid)
    assert np.allclose(current.u_mps, 0.01)
    assert np.allclose(current.v_mps, 0.02)


def test_netcdf_missing_variable_error(tmp_path: Path):
    pytest.importorskip("xarray")
    path = tmp_path / "currents.nc"
    _write_fixture(path, include_v=False)
    source = create_source("netcdf", input_netcdf=path)
    bbox = BoundingBox(-7.0, 51.5, -6.0, 52.5)
    grid = build_regular_grid(bbox, 0.5)
    with pytest.raises(ValidationError, match="v current variable"):
        source.get_current_grid(bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid)


def test_netcdf_bbox_coverage_error(tmp_path: Path):
    pytest.importorskip("xarray")
    path = tmp_path / "currents.nc"
    _write_fixture(path)
    source = create_source("netcdf", input_netcdf=path)
    bbox = BoundingBox(-8.0, 51.5, -6.0, 52.5)
    grid = build_regular_grid(bbox, 0.5)
    with pytest.raises(ValidationError, match="longitude range"):
        source.get_current_grid(bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid)


def test_netcdf_time_coverage_error(tmp_path: Path):
    pytest.importorskip("xarray")
    path = tmp_path / "currents.nc"
    _write_fixture(path)
    source = create_source("netcdf", input_netcdf=path)
    bbox = BoundingBox(-7.0, 51.5, -6.0, 52.5)
    grid = build_regular_grid(bbox, 0.5)
    with pytest.raises(ValidationError, match="outside source time range"):
        source.get_current_grid(bbox, parse_utc_datetime("2026-07-02T00:00:00Z"), grid)


def test_cli_netcdf_dry_run(tmp_path: Path, capsys):
    path = tmp_path / "currents.nc"
    rc = main(
        [
            "generate",
            "--bbox",
            "-7.0",
            "51.5",
            "-6.0",
            "52.5",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "1",
            "--step-hours",
            "1",
            "--grid-spacing-deg",
            "0.5",
            "--source",
            "netcdf",
            "--input-netcdf",
            str(path),
            "--output",
            str(tmp_path / "out.grb"),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert "source: netcdf" in capsys.readouterr().out
