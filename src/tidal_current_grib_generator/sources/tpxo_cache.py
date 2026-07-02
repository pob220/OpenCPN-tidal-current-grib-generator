"""Local derived TPXO harmonic-current cache."""

from __future__ import annotations

import json
import tempfile
import time as monotonic_time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, RegularGrid, build_regular_grid
from tidal_current_grib_generator.model import CurrentGrid
from tidal_current_grib_generator.sources.base import CurrentSource, SourceDescription
from tidal_current_grib_generator.sources.pytmd import inspect_pytmd_source

CACHE_FORMAT_VERSION = 1
CACHE_NOTICE = "Derived from local licensed TPXO model files. Do not redistribute unless your TPXO licence permits it."


@dataclass(frozen=True)
class TPXOCacheMetadata:
    bbox: BoundingBox
    grid_spacing_deg: float
    model_name: str
    pyTMD_version: str | None
    constituents: list[str]
    corrections: str
    minor_constituents: list[str]
    model_files: list[dict[str, Any]]
    created_utc: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": "tidal-current-grib-generator-tpxo-cache",
            "format_version": CACHE_FORMAT_VERSION,
            "notice": CACHE_NOTICE,
            "bbox": {
                "west": self.bbox.west,
                "south": self.bbox.south,
                "east": self.bbox.east,
                "north": self.bbox.north,
            },
            "grid_spacing_deg": self.grid_spacing_deg,
            "model_name": self.model_name,
            "pyTMD_version": self.pyTMD_version,
            "constituents": self.constituents,
            "corrections": self.corrections,
            "minor_constituents": self.minor_constituents,
            "model_files": self.model_files,
            "created_utc": self.created_utc,
        }


@dataclass
class PreparedTPXOCache:
    path: Path
    metadata: TPXOCacheMetadata
    grid: RegularGrid
    preparation_seconds: float
    point_count: int

    def summary(self) -> dict[str, Any]:
        return {
            "cache_file": str(self.path),
            "bbox": self.metadata.as_dict()["bbox"],
            "grid_spacing_deg": self.metadata.grid_spacing_deg,
            "grid_size": {"nx": self.grid.nx, "ny": self.grid.ny},
            "point_count": self.point_count,
            "model_name": self.metadata.model_name,
            "pyTMD_version": self.metadata.pyTMD_version,
            "constituents": self.metadata.constituents,
            "preparation_seconds": self.preparation_seconds,
            "notice": CACHE_NOTICE,
        }


@dataclass(frozen=True)
class TPXOCacheSource(CurrentSource):
    """Current source backed by locally cached TPXO harmonic constants."""

    input_cache: Path
    infer_minor: bool = True
    _cache: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)
    _last_timing: dict[str, Any] = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_cache", self.input_cache.expanduser())
        cache = load_tpxo_cache(self.input_cache)
        object.__setattr__(self, "_cache", cache)

    @property
    def bbox(self) -> BoundingBox:
        return self._cache["bbox"]

    @property
    def grid(self) -> RegularGrid:
        return self._cache["grid"]

    def describe(self) -> SourceDescription:
        return SourceDescription(
            name="tpxo-cache",
            summary="Cached TPXO10 astronomical tide model current source.",
            data_notice=CACHE_NOTICE,
        )

    def inspect(self) -> dict[str, Any]:
        metadata = dict(self._cache["metadata"])
        metadata["input_cache"] = str(self.input_cache)
        metadata["grid_size"] = {"nx": self.grid.nx, "ny": self.grid.ny}
        metadata["point_count"] = self.grid.nx * self.grid.ny
        metadata["valid"] = True
        return metadata

    def get_current_grid(self, bbox: BoundingBox, time: datetime, grid: RegularGrid) -> CurrentGrid:
        return self.get_current_grids(bbox, [time], grid)[0]

    def get_current_grids(self, bbox: BoundingBox, times: list[datetime], grid: RegularGrid) -> list[CurrentGrid]:
        _assert_cache_grid_matches(self.grid, grid)
        _assert_bbox_matches(self.bbox, bbox)
        if not times:
            return []
        started = monotonic_time.perf_counter()
        u_cm_s, v_cm_s = predict_from_cache(self._cache, times, infer_minor=self.infer_minor)
        predict_seconds = monotonic_time.perf_counter() - started
        grids: list[CurrentGrid] = []
        for index, valid_time in enumerate(times):
            u_mps = u_cm_s[index] / 100.0
            v_mps = v_cm_s[index] / 100.0
            mask = np.isnan(u_mps) | np.isnan(v_mps)
            grids.append(
                CurrentGrid(
                    time=valid_time.astimezone(timezone.utc),
                    grid=grid,
                    u_mps=np.where(mask, 0.0, u_mps),
                    v_mps=np.where(mask, 0.0, v_mps),
                    mask=mask if mask.any() else None,
                )
            )
        object.__setattr__(
            self,
            "_last_timing",
            {
                "source": "tpxo-cache",
                "coordinate_grid_size": {"nx": grid.nx, "ny": grid.ny},
                "point_count": grid.nx * grid.ny,
                "timestep_count": len(times),
                "cache_predict_seconds": predict_seconds,
            },
        )
        return grids

    def last_timing(self) -> dict[str, Any]:
        return dict(self._last_timing)


def prepare_tpxo_cache(
    *,
    bbox: BoundingBox,
    grid_spacing_deg: float,
    model_directory: Path,
    model_name: str,
    output: Path,
    definition_file: Path | None = None,
    interpolation_method: str = "linear",
    extrapolate: bool = False,
    extrapolation_cutoff_km: float = 10.0,
    crop_buffer_degrees: float = 1.0,
    verbose: bool = False,
) -> PreparedTPXOCache:
    started = monotonic_time.perf_counter()
    model_directory = model_directory.expanduser()
    output = output.expanduser()
    _require_pytmd_cache_dependencies()
    import pyTMD.io
    import xarray as xr

    grid = build_regular_grid(bbox, grid_spacing_deg)
    lon2d, lat2d = np.meshgrid(grid.longitudes, grid.latitudes)
    lon_points = lon2d.ravel()
    lat_points = lat2d.ravel()
    if verbose:
        print(
            f"preparing TPXO cache grid: {grid.nx} x {grid.ny} ({lon_points.size} points)",
            flush=True,
        )

    model_factory = pyTMD.io.model(model_directory, verify=False)
    model = (
        model_factory.from_file(definition_file.expanduser())
        if definition_file is not None
        else model_factory.from_database(model_name, group=("u", "v"))
    )
    dtree = model.open_datatree(group=["u", "v"], chunks="auto")
    x, y = dtree.tmd.coords_as(lon_points, lat_points, type="drift", crs=4326)
    dtree = dtree.tmd.crop([bbox.west, bbox.east, bbox.south, bbox.north], buffer=crop_buffer_degrees)

    components: dict[str, xr.Dataset] = {}
    model_files: list[dict[str, Any]] = []
    constituents: list[str] = []
    for key, node in dtree.items():
        component_started = monotonic_time.perf_counter()
        local = node.to_dataset().tmd.interp(
            x,
            y,
            method=interpolation_method,
            extrapolate=extrapolate,
            cutoff=extrapolation_cutoff_km,
        )
        local_constituents = list(local.tmd.constituents)
        if not constituents:
            constituents = local_constituents
        elif local_constituents != constituents:
            raise ValidationError("TPXO u/v constituent lists differ after interpolation; cannot build cache")
        components[key] = _component_dataset_for_cache(local, lon_points, lat_points, constituents)
        model_files.extend(_model_file_metadata(model_directory, local.attrs.get("lineage", [])))
        if verbose:
            elapsed = monotonic_time.perf_counter() - component_started
            print(f"interpolated TPXO {key} harmonic constants in {elapsed:.2f}s", flush=True)

    if "u" not in components or "v" not in components:
        raise ValidationError("TPXO model did not provide both u and v current components")

    metadata = TPXOCacheMetadata(
        bbox=bbox,
        grid_spacing_deg=grid_spacing_deg,
        model_name=model_name,
        pyTMD_version=_pytmd_version(),
        constituents=constituents,
        corrections=str(getattr(model, "corrections", "ATLAS")),
        minor_constituents=[str(c) for c in (getattr(model, "minor", None) or [])],
        model_files=_dedupe_model_files(model_files),
        created_utc=datetime.now(timezone.utc).isoformat(),
    )
    _write_cache_atomic(output, metadata, grid, lon_points, lat_points, components)
    return PreparedTPXOCache(
        path=output,
        metadata=metadata,
        grid=grid,
        preparation_seconds=monotonic_time.perf_counter() - started,
        point_count=lon_points.size,
    )


def load_tpxo_cache(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if not path.exists():
        raise ValidationError(f"TPXO cache file does not exist: {path}")
    _require_pytmd_cache_dependencies()
    import xarray as xr

    try:
        with np.load(path, allow_pickle=False) as data:
            metadata = json.loads(str(data["metadata_json"]))
            if metadata.get("format") != "tidal-current-grib-generator-tpxo-cache":
                raise ValidationError("not a tidal-current-grib-generator TPXO cache file")
            if int(metadata.get("format_version", 0)) != CACHE_FORMAT_VERSION:
                raise ValidationError(
                    f"unsupported TPXO cache format version {metadata.get('format_version')}; "
                    f"expected {CACHE_FORMAT_VERSION}"
                )
            longitudes = np.asarray(data["longitudes"], dtype=np.float64)
            latitudes = np.asarray(data["latitudes"], dtype=np.float64)
            lon_points = np.asarray(data["lon_points"], dtype=np.float64)
            lat_points = np.asarray(data["lat_points"], dtype=np.float64)
            constituents = [str(c) for c in np.asarray(data["constituents"]).tolist()]
            u = np.asarray(data["u_complex"], dtype=np.complex128)
            v = np.asarray(data["v_complex"], dtype=np.complex128)
    except ValidationError:
        raise
    except Exception as exc:
        raise ValidationError(f"could not read TPXO cache file {path}: {exc}") from exc

    if u.shape != v.shape:
        raise ValidationError("TPXO cache u/v arrays have different shapes")
    if u.shape != (len(constituents), lon_points.size):
        raise ValidationError(
            f"TPXO cache arrays have shape {u.shape}, expected {(len(constituents), lon_points.size)}"
        )
    bbox_info = metadata["bbox"]
    bbox = BoundingBox(
        float(bbox_info["west"]),
        float(bbox_info["south"]),
        float(bbox_info["east"]),
        float(bbox_info["north"]),
    )
    grid = RegularGrid(
        latitudes=latitudes,
        longitudes=longitudes,
        spacing_deg=float(metadata["grid_spacing_deg"]),
        latitude_spacing_deg=float(metadata["grid_spacing_deg"]),
        longitude_spacing_deg=float(metadata["grid_spacing_deg"]),
    )
    attrs = {"crs": {"proj": "longlat", "datum": "WGS84", "ellps": "WGS84", "lon_wrap": 180, "type": "crs"}}
    u_ds = _dataset_from_cached_component(xr, "u", constituents, u, lon_points, lat_points, attrs)
    v_ds = _dataset_from_cached_component(xr, "v", constituents, v, lon_points, lat_points, attrs)
    return {
        "metadata": metadata,
        "bbox": bbox,
        "grid": grid,
        "u": u_ds,
        "v": v_ds,
        "constituents": constituents,
    }


def predict_from_cache(cache: dict[str, Any], times: list[datetime], *, infer_minor: bool = True) -> tuple[np.ndarray, np.ndarray]:
    import timescale

    valid_datetimes = [
        np.datetime64(t.astimezone(timezone.utc).replace(tzinfo=None), "ns")
        for t in times
    ]
    ts = timescale.from_datetime(np.asarray(valid_datetimes, dtype="datetime64[ns]"))
    corrections = str(cache["metadata"].get("corrections") or "ATLAS")
    deltat = np.zeros_like(ts.tt_ut1) if corrections in ("OTIS", "ATLAS", "TMD3", "netcdf") else ts.tt_ut1
    minor = cache["metadata"].get("minor_constituents") or None
    u = _predict_component(cache["u"], ts.tide, deltat, corrections, minor, infer_minor)
    v = _predict_component(cache["v"], ts.tide, deltat, corrections, minor, infer_minor)
    expected = (len(times), cache["grid"].ny, cache["grid"].nx)
    return _reshape_cached_prediction(u, expected), _reshape_cached_prediction(v, expected)


def _predict_component(ds: Any, tide_time: Any, deltat: Any, corrections: str, minor: list[str] | None, infer_minor: bool) -> np.ndarray:
    predicted = ds.tmd.predict(tide_time, deltat=deltat, corrections=corrections)
    if infer_minor:
        predicted = predicted + ds.tmd.infer(tide_time, deltat=deltat, corrections=corrections, minor=minor)
    return np.asarray(predicted.values, dtype=np.float64)


def _reshape_cached_prediction(values: np.ndarray, expected: tuple[int, int, int]) -> np.ndarray:
    nt, ny, nx = expected
    npoints = ny * nx
    values = np.squeeze(values)
    if values.shape == (npoints, nt):
        return values.T.reshape(expected)
    if values.shape == (nt, npoints):
        return values.reshape(expected)
    if values.shape == (npoints,) and nt == 1:
        return values.reshape((1, ny, nx))
    if values.shape == expected:
        return values
    raise ValidationError(f"TPXO cache prediction shape {values.shape} cannot be reshaped to {expected}")


def _component_dataset_for_cache(local: Any, lon_points: np.ndarray, lat_points: np.ndarray, constituents: list[str]) -> Any:
    import xarray as xr

    data_vars = {}
    for constituent in constituents:
        values = np.asarray(local[constituent].values, dtype=np.complex128)
        values = np.squeeze(values)
        if values.shape != (lon_points.size,):
            raise ValidationError(
                f"interpolated TPXO constituent {constituent!r} has shape {values.shape}, "
                f"expected {(lon_points.size,)}"
            )
        data_vars[constituent] = (("point",), values, dict(local[constituent].attrs))
    attrs = dict(local.attrs)
    attrs.setdefault("crs", {"proj": "longlat", "datum": "WGS84", "ellps": "WGS84", "lon_wrap": 180, "type": "crs"})
    return xr.Dataset(
        data_vars=data_vars,
        coords={"x": (("point",), lon_points), "y": (("point",), lat_points)},
        attrs=attrs,
    )


def _dataset_from_cached_component(
    xr: Any,
    component: str,
    constituents: list[str],
    values: np.ndarray,
    lon_points: np.ndarray,
    lat_points: np.ndarray,
    attrs: dict[str, Any],
) -> Any:
    data_vars = {}
    for index, constituent in enumerate(constituents):
        data_vars[constituent] = (("point",), values[index], {"units": "cm/s"})
    ds_attrs = dict(attrs)
    ds_attrs["group"] = component
    return xr.Dataset(
        data_vars=data_vars,
        coords={"x": (("point",), lon_points), "y": (("point",), lat_points)},
        attrs=ds_attrs,
    )


def _write_cache_atomic(
    output: Path,
    metadata: TPXOCacheMetadata,
    grid: RegularGrid,
    lon_points: np.ndarray,
    lat_points: np.ndarray,
    components: dict[str, Any],
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            np.savez_compressed(
                tmp,
                metadata_json=np.array(json.dumps(metadata.as_dict(), sort_keys=True)),
                longitudes=grid.longitudes,
                latitudes=grid.latitudes,
                lon_points=lon_points,
                lat_points=lat_points,
                constituents=np.asarray(metadata.constituents),
                u_complex=_component_matrix(components["u"], metadata.constituents),
                v_complex=_component_matrix(components["v"], metadata.constituents),
            )
        tmp_path.replace(output)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def _component_matrix(ds: Any, constituents: list[str]) -> np.ndarray:
    return np.vstack([np.asarray(ds[c].values, dtype=np.complex128) for c in constituents])


def _model_file_metadata(model_directory: Path, lineage: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in lineage:
        if not isinstance(name, str):
            continue
        matches = list(model_directory.rglob(name))
        for path in matches[:1]:
            try:
                stat = path.stat()
            except OSError:
                continue
            records.append(
                {
                    "name": path.name,
                    "relative_path": str(path.relative_to(model_directory)),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                }
            )
    return records


def _dedupe_model_files(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for record in records:
        deduped[record["relative_path"]] = record
    return [deduped[key] for key in sorted(deduped)]


def _pytmd_version() -> str | None:
    try:
        import pyTMD.version

        return str(getattr(pyTMD.version, "version", None) or getattr(pyTMD.version, "full_version", None))
    except Exception:
        return None


def _require_pytmd_cache_dependencies() -> None:
    try:
        import pyTMD.io.dataset  # noqa: F401
        import timescale  # noqa: F401
        import xarray  # noqa: F401
    except ImportError as exc:
        raise MissingDependencyError(
            "TPXO cache support requires pyTMD, timescale and xarray. "
            "Install TPXO support with `pip install tidal-current-grib-generator[tpxo]`."
        ) from exc


def _assert_cache_grid_matches(cache_grid: RegularGrid, requested_grid: RegularGrid) -> None:
    if cache_grid.nx != requested_grid.nx or cache_grid.ny != requested_grid.ny:
        raise ValidationError("requested grid does not match TPXO cache grid")
    if not np.allclose(cache_grid.longitudes, requested_grid.longitudes) or not np.allclose(
        cache_grid.latitudes, requested_grid.latitudes
    ):
        raise ValidationError("requested grid coordinates do not match TPXO cache grid")


def _assert_bbox_matches(cache_bbox: BoundingBox, requested_bbox: BoundingBox) -> None:
    if (
        abs(cache_bbox.west - requested_bbox.west) > 1e-10
        or abs(cache_bbox.east - requested_bbox.east) > 1e-10
        or abs(cache_bbox.south - requested_bbox.south) > 1e-10
        or abs(cache_bbox.north - requested_bbox.north) > 1e-10
    ):
        raise ValidationError("requested bbox does not match TPXO cache bbox")


def validate_tpxo_cache(path: Path) -> dict[str, Any]:
    cache = load_tpxo_cache(path)
    inspection = dict(cache["metadata"])
    inspection["input_cache"] = str(path.expanduser())
    inspection["grid_size"] = {"nx": cache["grid"].nx, "ny": cache["grid"].ny}
    inspection["point_count"] = cache["grid"].nx * cache["grid"].ny
    inspection["valid"] = True
    stale = _stale_model_files(path.expanduser(), inspection.get("model_files", []))
    inspection["stale_model_files"] = stale
    inspection["stale"] = bool(stale)
    return inspection


def _stale_model_files(cache_path: Path, model_files: list[dict[str, Any]]) -> list[str]:
    # Cache files can move between machines; stale checks are best-effort unless
    # the original relative source metadata is still resolvable by the user.
    _ = cache_path
    stale: list[str] = []
    for record in model_files:
        if not {"relative_path", "size", "mtime"} <= set(record):
            stale.append(str(record.get("name", "(unknown)")))
    return stale
