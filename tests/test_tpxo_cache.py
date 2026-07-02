from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from tidal_current_grib_generator.errors import ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid
from tidal_current_grib_generator.sources import create_source
from tidal_current_grib_generator.sources.tpxo_cache import (
    CACHE_NOTICE,
    TPXOCacheMetadata,
    _write_cache_atomic,
    load_tpxo_cache,
    predict_from_cache,
    validate_tpxo_cache,
)


pytest.importorskip("xarray")
pytest.importorskip("pyTMD")


def _write_fake_cache(path: Path, *, stale: bool = False) -> None:
    import xarray as xr

    bbox = BoundingBox(-1.0, 50.0, -0.9, 50.1)
    grid = build_regular_grid(bbox, 0.1)
    lon2d, lat2d = np.meshgrid(grid.longitudes, grid.latitudes)
    lon_points = lon2d.ravel()
    lat_points = lat2d.ravel()
    constituents = ["m2"]
    model_files = [{"name": "missing.nc"}] if stale else []
    metadata = TPXOCacheMetadata(
        bbox=bbox,
        grid_spacing_deg=0.1,
        model_name="fake-tpxo",
        pyTMD_version="test",
        constituents=constituents,
        corrections="ATLAS",
        minor_constituents=[],
        model_files=model_files,
        created_utc=datetime.now(timezone.utc).isoformat(),
    )
    attrs = {"crs": {"proj": "longlat", "datum": "WGS84", "ellps": "WGS84", "lon_wrap": 180, "type": "crs"}}
    dataset = xr.Dataset(
        {"m2": (("point",), np.ones(lon_points.size, dtype=np.complex128), {"units": "cm/s"})},
        coords={"x": (("point",), lon_points), "y": (("point",), lat_points)},
        attrs=attrs,
    )
    _write_cache_atomic(path, metadata, grid, lon_points, lat_points, {"u": dataset, "v": dataset})


def test_tpxo_cache_metadata_roundtrip(tmp_path: Path):
    cache_path = tmp_path / "test.tpxocache"
    _write_fake_cache(cache_path)

    cache = load_tpxo_cache(cache_path)

    assert cache["metadata"]["model_name"] == "fake-tpxo"
    assert cache["metadata"]["notice"] == CACHE_NOTICE
    assert cache["grid"].nx == 2
    assert cache["grid"].ny == 2
    assert cache["constituents"] == ["m2"]


def test_tpxo_cache_predicts_current_grid(tmp_path: Path):
    cache_path = tmp_path / "test.tpxocache"
    _write_fake_cache(cache_path)
    source = create_source("tpxo-cache", input_cache=cache_path)

    current = source.get_current_grid(
        source.bbox,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        source.grid,
    )

    assert current.u_mps.shape == source.grid.shape
    assert current.v_mps.shape == source.grid.shape
    assert np.isfinite(current.u_mps).all()
    assert np.isfinite(current.v_mps).all()


def test_tpxo_cache_numerical_prediction_is_stable(tmp_path: Path):
    cache_path = tmp_path / "test.tpxocache"
    _write_fake_cache(cache_path)
    cache = load_tpxo_cache(cache_path)
    times = [datetime(2026, 1, 1, hour=hour, tzinfo=timezone.utc) for hour in range(3)]

    first_u, first_v = predict_from_cache(cache, times, infer_minor=False)
    second_u, second_v = predict_from_cache(cache, times, infer_minor=False)

    assert first_u.shape == (3, 2, 2)
    assert np.allclose(first_u, second_u)
    assert np.allclose(first_v, second_v)


def test_tpxo_cache_invalid_file_rejected(tmp_path: Path):
    cache_path = tmp_path / "bad.tpxocache"
    cache_path.write_text("not a cache")

    with pytest.raises(ValidationError, match="could not read TPXO cache"):
        load_tpxo_cache(cache_path)


def test_tpxo_cache_stale_metadata_detection(tmp_path: Path):
    cache_path = tmp_path / "stale.tpxocache"
    _write_fake_cache(cache_path, stale=True)

    inspection = validate_tpxo_cache(cache_path)

    assert inspection["valid"] is True
    assert inspection["stale"] is True
    assert inspection["stale_model_files"] == ["missing.nc"]
