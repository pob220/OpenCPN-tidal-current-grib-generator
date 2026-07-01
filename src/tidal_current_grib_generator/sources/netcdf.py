"""Local NetCDF current source."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, RegularGrid
from tidal_current_grib_generator.model import CurrentGrid
from tidal_current_grib_generator.sources.base import CurrentSource, SourceDescription

U_CANDIDATES = (
    "eastward_sea_water_velocity",
    "uo",
    "surface_eastward_sea_water_velocity",
    "barotropic_eastward_sea_water_velocity",
)
V_CANDIDATES = (
    "northward_sea_water_velocity",
    "vo",
    "surface_northward_sea_water_velocity",
    "barotropic_northward_sea_water_velocity",
)
LAT_CANDIDATES = ("latitude", "lat", "nav_lat", "y")
LON_CANDIDATES = ("longitude", "lon", "nav_lon", "x")
TIME_CANDIDATES = ("time", "datetime")
DEPTH_CANDIDATES = ("depth", "depthu", "depthv", "lev", "level")


def xarray_is_available() -> bool:
    return importlib.util.find_spec("xarray") is not None


def _import_xarray():
    try:
        import xarray as xr
    except ImportError as exc:
        raise MissingDependencyError(
            "NetCDF support requires xarray. Install it with "
            "`pip install tidal-current-grib-generator[netcdf]`."
        ) from exc
    return xr


@dataclass(frozen=True)
class NetCDFCurrentSource(CurrentSource):
    """Read u/v current components from a local NetCDF file using xarray."""

    input_netcdf: Path
    u_variable: str | None = None
    v_variable: str | None = None
    lat_variable: str | None = None
    lon_variable: str | None = None
    time_variable: str | None = None
    depth_index: int | None = None
    depth_value: float | None = None
    assume_units: str | None = None
    nearest_time: bool = False
    coverage_tolerance_deg: float = 0.02
    use_source_grid: bool = False
    source_grid_regularity_tolerance: float = 1e-5

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_netcdf", self.input_netcdf.expanduser())

    def describe(self) -> SourceDescription:
        return SourceDescription(
            name="netcdf",
            summary=f"Local NetCDF current file: {self.input_netcdf}",
            data_notice="Local model data; accuracy and licence depend on the file source.",
        )

    def validate_available(self) -> None:
        if not self.input_netcdf.exists():
            raise ValidationError(f"NetCDF file does not exist: {self.input_netcdf}")
        if not self.input_netcdf.is_file():
            raise ValidationError(f"NetCDF path is not a file: {self.input_netcdf}")
        _import_xarray()

    def get_current_grid(self, bbox: BoundingBox, time: datetime, grid: RegularGrid) -> CurrentGrid:
        self.validate_available()
        xr = _import_xarray()
        with xr.open_dataset(self.input_netcdf) as dataset:
            spec = _detect_spec(
                dataset,
                self.u_variable,
                self.v_variable,
                self.lat_variable,
                self.lon_variable,
                self.time_variable,
            )
            _assert_spatial_coverage(dataset, spec, bbox, self.coverage_tolerance_deg)
            data_u = _select_depth(dataset[spec.u_name], dataset, spec, self.depth_index, self.depth_value)
            data_v = _select_depth(dataset[spec.v_name], dataset, spec, self.depth_index, self.depth_value)
            data_u = _select_time(data_u, spec.time_name, time, self.nearest_time)
            data_v = _select_time(data_v, spec.time_name, time, self.nearest_time)
            data_u = _prepare_spatial(data_u, spec)
            data_v = _prepare_spatial(data_v, spec)
            target_lons = _target_longitudes_for_source(grid.longitudes, dataset[spec.lon_name].values)
            if self.use_source_grid:
                selection = {spec.lat_name: grid.latitudes, spec.lon_name: target_lons}
                data_u = data_u.sel(selection)
                data_v = data_v.sel(selection)
            interpolation_used = _grid_differs_from_source(
                grid.latitudes,
                target_lons,
                np.asarray(data_u[spec.lat_name].values, dtype=float),
                np.asarray(data_u[spec.lon_name].values, dtype=float),
            )
            if interpolation_used:
                interp_kwargs = {
                    spec.lat_name: grid.latitudes,
                    spec.lon_name: target_lons,
                }
                u_interp = data_u.interp(interp_kwargs)
                v_interp = data_v.interp(interp_kwargs)
            else:
                u_interp = data_u
                v_interp = data_v
            u_values = _array_values(u_interp, grid.shape)
            v_values = _array_values(v_interp, grid.shape)
            u_values = _convert_units(u_values, data_u.attrs.get("units"), self.assume_units, spec.u_name)
            v_values = _convert_units(v_values, data_v.attrs.get("units"), self.assume_units, spec.v_name)
            mask = np.isnan(u_values) | np.isnan(v_values)
            return CurrentGrid(
                time=time.astimezone(timezone.utc),
                grid=grid,
                u_mps=np.where(mask, 0.0, u_values),
                v_mps=np.where(mask, 0.0, v_values),
                mask=mask if mask.any() else None,
            )

    def inspect(self) -> dict[str, Any]:
        return inspect_netcdf(self.input_netcdf)

    def source_bounds(self) -> BoundingBox:
        self.validate_available()
        xr = _import_xarray()
        with xr.open_dataset(self.input_netcdf) as dataset:
            spec = _detect_spec(
                dataset,
                self.u_variable,
                self.v_variable,
                self.lat_variable,
                self.lon_variable,
                self.time_variable,
            )
            lat_min, lat_max = _coord_range(dataset[spec.lat_name].values)
            lon_min, lon_max = _coord_range(_display_longitudes(dataset[spec.lon_name].values))
            return BoundingBox(lon_min, lat_min, lon_max, lat_max)

    def clip_bbox_to_source(self, bbox: BoundingBox) -> BoundingBox:
        source = self.source_bounds()
        clipped = BoundingBox(
            max(bbox.west, source.west),
            max(bbox.south, source.south),
            min(bbox.east, source.east),
            min(bbox.north, source.north),
        )
        clipped.validate()
        return clipped

    def build_source_grid(self, bbox: BoundingBox) -> RegularGrid:
        self.validate_available()
        xr = _import_xarray()
        with xr.open_dataset(self.input_netcdf) as dataset:
            spec = _detect_spec(
                dataset,
                self.u_variable,
                self.v_variable,
                self.lat_variable,
                self.lon_variable,
                self.time_variable,
            )
            _assert_spatial_coverage(dataset, spec, bbox, self.coverage_tolerance_deg)
            lats = np.asarray(dataset[spec.lat_name].values, dtype=float)
            lons = _display_longitudes(dataset[spec.lon_name].values)
            lats = np.sort(lats[(lats >= bbox.south) & (lats <= bbox.north)])
            lons = np.sort(lons[(lons >= bbox.west) & (lons <= bbox.east)])
            if lats.size < 2 or lons.size < 2:
                raise ValidationError("source grid selection must contain at least two latitude and longitude points")
            lat_spacing = _regular_spacing(lats, "latitude", self.source_grid_regularity_tolerance)
            lon_spacing = _regular_spacing(lons, "longitude", self.source_grid_regularity_tolerance)
            return RegularGrid(
                latitudes=lats,
                longitudes=lons,
                spacing_deg=float(max(abs(lat_spacing), abs(lon_spacing))),
                latitude_spacing_deg=float(abs(lat_spacing)),
                longitude_spacing_deg=float(abs(lon_spacing)),
            )

    def metadata(self, bbox: BoundingBox, grid: RegularGrid) -> dict[str, Any]:
        data = inspect_netcdf(self.input_netcdf)
        source_bounds = self.source_bounds()
        detected_u = data.get("detected_u_variable")
        detected_v = data.get("detected_v_variable")
        units = data.get("variable_units", {})
        source_lats = None
        source_lons = None
        interpolation_used = True
        try:
            xr = _import_xarray()
            with xr.open_dataset(self.input_netcdf) as dataset:
                spec = _detect_spec(
                    dataset,
                    self.u_variable,
                    self.v_variable,
                    self.lat_variable,
                    self.lon_variable,
                    self.time_variable,
                )
                source_lats = np.asarray(dataset[spec.lat_name].values, dtype=float)
                source_lons = _display_longitudes(dataset[spec.lon_name].values)
                interpolation_used = _grid_differs_from_source(grid.latitudes, grid.longitudes, source_lats, source_lons)
        except Exception:
            interpolation_used = not self.use_source_grid
        return {
            "input_file": str(self.input_netcdf),
            "source_bounds": {
                "west": source_bounds.west,
                "south": source_bounds.south,
                "east": source_bounds.east,
                "north": source_bounds.north,
            },
            "u_variable": detected_u,
            "v_variable": detected_v,
            "units": {
                "u": units.get(detected_u, "") if detected_u else "",
                "v": units.get(detected_v, "") if detected_v else "",
            },
            "interpolation_used": interpolation_used,
        }


@dataclass(frozen=True)
class _NetCDFSpec:
    u_name: str
    v_name: str
    lat_name: str
    lon_name: str
    time_name: str


def inspect_netcdf(path: Path) -> dict[str, Any]:
    path = path.expanduser()
    if not path.exists():
        raise ValidationError(f"NetCDF file does not exist: {path}")
    xr = _import_xarray()
    with xr.open_dataset(path) as dataset:
        likely_u = [name for name in U_CANDIDATES if name in dataset.data_vars]
        likely_v = [name for name in V_CANDIDATES if name in dataset.data_vars]
        coord_names = list(dataset.coords)
        dimensions = {name: int(size) for name, size in dataset.sizes.items()}
        variables = {name: str(dataset[name].attrs.get("units", "")) for name in dataset.data_vars}
        result: dict[str, Any] = {
            "path": str(path),
            "dimensions": dimensions,
            "coordinate_variables": coord_names,
            "likely_u_variables": likely_u,
            "likely_v_variables": likely_v,
            "variable_units": variables,
        }
        try:
            spec = _detect_spec(dataset, None, None, None, None, None)
            result["latitude_range"] = _coord_range(dataset[spec.lat_name].values)
            result["longitude_range"] = _coord_range(dataset[spec.lon_name].values)
            result["time_range"] = _time_range(dataset[spec.time_name].values)
            result["detected_u_variable"] = spec.u_name
            result["detected_v_variable"] = spec.v_name
        except ValidationError as exc:
            result["detection_error"] = str(exc)
        depths = {}
        for name in DEPTH_CANDIDATES:
            if name in dataset.coords or name in dataset.dims:
                values = dataset[name].values if name in dataset else np.arange(dataset.sizes[name])
                depths[name] = [float(v) for v in np.asarray(values).ravel()[:50]]
        result["depth_levels"] = depths
        return result


def netcdf_time_metadata(path: Path) -> dict[str, Any]:
    """Return parsed UTC time-coordinate metadata for generation alignment."""

    path = path.expanduser()
    if not path.exists():
        raise ValidationError(f"NetCDF file does not exist: {path}")
    xr = _import_xarray()
    with xr.open_dataset(path) as dataset:
        spec = _detect_spec(dataset, None, None, None, None, None)
        values = np.asarray(dataset[spec.time_name].values)
        if values.size == 0:
            raise ValidationError("NetCDF time coordinate is empty")
        times = [_numpy_datetime_to_utc(value) for value in values]
        times = sorted(times)
        step_hours = None
        if len(times) > 1:
            deltas = [
                (right - left).total_seconds() / 3600.0
                for left, right in zip(times[:-1], times[1:])
            ]
            median = float(np.median(deltas))
            max_deviation = float(np.max(np.abs(np.asarray(deltas, dtype=float) - median)))
            step_hours = median
            if max_deviation > max(1e-6, abs(median) * 1e-6):
                raise ValidationError(
                    "NetCDF time coordinate is not regular enough for automatic Copernicus generation "
                    f"(median step {median:g} h, max deviation {max_deviation:g} h)"
                )
        return {
            "first_time": times[0],
            "last_time": times[-1],
            "time_count": len(times),
            "step_hours": step_hours,
            "times": times,
        }


def _numpy_datetime_to_utc(value: Any) -> datetime:
    scalar = np.asarray(value)
    if np.issubdtype(scalar.dtype, np.datetime64):
        text = np.datetime_as_string(scalar.astype("datetime64[us]"), unit="us", timezone="UTC")
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    parsed = np.datetime64(value, "us")
    text = np.datetime_as_string(parsed, unit="us", timezone="UTC")
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


def _detect_spec(
    dataset: Any,
    u_name: str | None,
    v_name: str | None,
    lat_name: str | None,
    lon_name: str | None,
    time_name: str | None,
) -> _NetCDFSpec:
    u = u_name or _first_present(dataset.data_vars, U_CANDIDATES, "u current variable")
    v = v_name or _first_present(dataset.data_vars, V_CANDIDATES, "v current variable")
    lat = lat_name or _first_present(dataset.variables, LAT_CANDIDATES, "latitude coordinate")
    lon = lon_name or _first_present(dataset.variables, LON_CANDIDATES, "longitude coordinate")
    time = time_name or _first_present(dataset.variables, TIME_CANDIDATES, "time coordinate")
    for name, label in ((u, "u variable"), (v, "v variable"), (lat, "latitude"), (lon, "longitude"), (time, "time")):
        if name not in dataset:
            raise ValidationError(f"{label} {name!r} not found in NetCDF file")
    return _NetCDFSpec(u_name=u, v_name=v, lat_name=lat, lon_name=lon, time_name=time)


def _first_present(names: Any, candidates: tuple[str, ...], label: str) -> str:
    for candidate in candidates:
        if candidate in names:
            return candidate
    raise ValidationError(f"could not auto-detect {label}; provide it explicitly")


def _assert_spatial_coverage(
    dataset: Any,
    spec: _NetCDFSpec,
    bbox: BoundingBox,
    tolerance_deg: float = 0.02,
) -> None:
    lat_values = np.asarray(dataset[spec.lat_name].values, dtype=float)
    lon_values = np.asarray(dataset[spec.lon_name].values, dtype=float)
    source_west, source_east = _coord_range(_display_longitudes(lon_values))
    west, east = _target_longitudes_for_source(np.asarray([bbox.west, bbox.east]), lon_values)
    lat_min, lat_max = _coord_range(lat_values)
    if bbox.south < lat_min - tolerance_deg or bbox.north > lat_max + tolerance_deg:
        raise ValidationError(
            "requested bbox latitude range "
            f"[{bbox.south}, {bbox.north}] is outside source [{lat_min}, {lat_max}] "
            f"with tolerance {tolerance_deg} deg; use --clip-bbox-to-source or an inset bbox"
        )
    display_west, display_east = _display_longitudes(np.asarray([west, east], dtype=float))
    if float(display_west) < source_west - tolerance_deg or float(display_east) > source_east + tolerance_deg:
        raise ValidationError(
            "requested bbox longitude range "
            f"[{bbox.west}, {bbox.east}] is outside source [{source_west}, {source_east}] "
            f"with tolerance {tolerance_deg} deg; use --clip-bbox-to-source or an inset bbox"
        )


def _select_depth(data: Any, dataset: Any, spec: _NetCDFSpec, depth_index: int | None, depth_value: float | None) -> Any:
    depth_dims = [
        dim for dim in data.dims if dim not in {spec.time_name, spec.lat_name, spec.lon_name}
    ]
    if not depth_dims:
        return data
    if len(depth_dims) > 1:
        raise ValidationError(f"unsupported extra dimensions on {data.name}: {depth_dims}")
    depth_dim = depth_dims[0]
    size = int(data.sizes[depth_dim])
    if depth_index is not None and depth_value is not None:
        raise ValidationError("use either --depth-index or --depth-value, not both")
    if depth_index is not None:
        if depth_index < 0 or depth_index >= size:
            raise ValidationError(f"--depth-index {depth_index} is outside available range 0..{size - 1}")
        return data.isel({depth_dim: depth_index})
    if depth_value is not None:
        if depth_dim not in dataset.coords:
            raise ValidationError(f"cannot use --depth-value because {depth_dim!r} has no coordinate values")
        return data.sel({depth_dim: depth_value}, method="nearest")
    if size == 1:
        return data.isel({depth_dim: 0})
    raise ValidationError(
        f"variable {data.name!r} has depth/extra dimension {depth_dim!r}; provide --depth-index or --depth-value"
    )


def _select_time(data: Any, time_name: str, time: datetime, nearest_time: bool) -> Any:
    if time_name not in data.dims:
        return data
    target = np.datetime64(time.astimezone(timezone.utc).replace(tzinfo=None))
    values = np.asarray(data[time_name].values)
    if values.size:
        start = values.min()
        end = values.max()
        if target < start or target > end:
            raise ValidationError(
                f"requested time {time.isoformat()} is outside source time range [{start}, {end}]"
            )
    try:
        return data.sel({time_name: target}, method="nearest" if nearest_time else None)
    except Exception as exc:
        raise ValidationError(
            f"requested time {time.isoformat()} is not available in NetCDF file; "
            "use --nearest-time to select the nearest source time"
        ) from exc


def _prepare_spatial(data: Any, spec: _NetCDFSpec) -> Any:
    if spec.lat_name not in data.dims or spec.lon_name not in data.dims:
        raise ValidationError(
            f"variable {data.name!r} must have latitude and longitude dimensions "
            f"{spec.lat_name!r}, {spec.lon_name!r}"
        )
    if data[spec.lat_name].values[0] > data[spec.lat_name].values[-1]:
        data = data.sortby(spec.lat_name)
    if data[spec.lon_name].values[0] > data[spec.lon_name].values[-1]:
        data = data.sortby(spec.lon_name)
    return data


def _target_longitudes_for_source(longitudes: np.ndarray, source_longitudes: np.ndarray) -> np.ndarray:
    source_min = float(np.nanmin(source_longitudes))
    source_max = float(np.nanmax(source_longitudes))
    if source_min >= 0.0 and source_max > 180.0:
        return np.mod(longitudes, 360.0)
    return longitudes


def _display_longitudes(longitudes: Any) -> np.ndarray:
    values = np.asarray(longitudes, dtype=float)
    if np.nanmin(values) >= 0.0 and np.nanmax(values) > 180.0:
        return ((values + 180.0) % 360.0) - 180.0
    return values


def _convert_units(values: np.ndarray, units_attr: Any, assume_units: str | None, variable: str) -> np.ndarray:
    units = _normalize_units(assume_units or str(units_attr or ""))
    if units == "mps":
        return values
    if units == "cmps":
        return values / 100.0
    raise ValidationError(
        f"unknown units for {variable!r}: {units_attr!r}; provide --assume-units mps or --assume-units cmps"
    )


def _normalize_units(value: str) -> str:
    normalized = value.strip().lower().replace(" ", "")
    if normalized in {"mps", "m/s", "ms-1", "m.s-1", "metersecond-1", "metresecond-1"}:
        return "mps"
    if normalized in {"cmps", "cm/s", "cms-1", "cm.s-1", "centimetersecond-1", "centimetresecond-1"}:
        return "cmps"
    return normalized


def _array_values(data: Any, expected_shape: tuple[int, int]) -> np.ndarray:
    values = np.ma.filled(np.ma.asarray(data.values, dtype=np.float64), np.nan)
    values = np.squeeze(values)
    if values.shape == expected_shape:
        return values
    if values.shape == (expected_shape[1], expected_shape[0]):
        return values.T
    raise ValidationError(f"interpolated NetCDF data has shape {values.shape}, expected {expected_shape}")


def _grid_differs_from_source(
    target_lats: np.ndarray,
    target_lons: np.ndarray,
    source_lats: np.ndarray,
    source_lons: np.ndarray,
) -> bool:
    if target_lats.size != source_lats.size or target_lons.size != source_lons.size:
        return True
    return not (np.allclose(target_lats, np.sort(source_lats)) and np.allclose(target_lons, np.sort(source_lons)))


def _regular_spacing(values: np.ndarray, label: str, tolerance: float = 1e-5) -> float:
    diffs = np.diff(np.asarray(values, dtype=float))
    if diffs.size == 0:
        raise ValidationError(f"{label} coordinate has too few points")
    spacing = float(np.median(diffs))
    max_abs_deviation = float(np.max(np.abs(diffs - spacing)))
    relative_deviation = max_abs_deviation / max(abs(spacing), 1e-12)
    allowed = max(float(tolerance), abs(spacing) * float(tolerance))
    if max_abs_deviation > allowed:
        raise ValidationError(
            f"{label} coordinate is not regular enough for GRIB output: "
            f"median spacing={spacing:.12g} deg, min spacing={float(np.min(diffs)):.12g}, "
            f"max spacing={float(np.max(diffs)):.12g}, max deviation={max_abs_deviation:.12g} deg "
            f"(relative {relative_deviation:.6g}), tolerance={tolerance:.12g}. "
            "Omit --use-source-grid to interpolate to a regular output grid, or increase "
            "--source-grid-regularity-tolerance if this is expected coordinate precision noise."
        )
    return spacing


def _coord_range(values: Any) -> tuple[float, float]:
    array = np.asarray(values, dtype=float)
    return float(np.nanmin(array)), float(np.nanmax(array))


def _time_range(values: Any) -> tuple[str, str]:
    array = np.asarray(values)
    if array.size == 0:
        return "", ""
    return str(array[0]), str(array[-1])


CopernicusNetCDFSource = NetCDFCurrentSource
