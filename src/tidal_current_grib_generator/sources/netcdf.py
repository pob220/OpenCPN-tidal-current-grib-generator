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
            _assert_spatial_coverage(dataset, spec, bbox)
            data_u = _select_depth(dataset[spec.u_name], dataset, spec, self.depth_index, self.depth_value)
            data_v = _select_depth(dataset[spec.v_name], dataset, spec, self.depth_index, self.depth_value)
            data_u = _select_time(data_u, spec.time_name, time, self.nearest_time)
            data_v = _select_time(data_v, spec.time_name, time, self.nearest_time)
            data_u = _prepare_spatial(data_u, spec)
            data_v = _prepare_spatial(data_v, spec)
            target_lons = _target_longitudes_for_source(grid.longitudes, dataset[spec.lon_name].values)
            interp_kwargs = {
                spec.lat_name: grid.latitudes,
                spec.lon_name: target_lons,
            }
            u_interp = data_u.interp(interp_kwargs)
            v_interp = data_v.interp(interp_kwargs)
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


def _assert_spatial_coverage(dataset: Any, spec: _NetCDFSpec, bbox: BoundingBox) -> None:
    lat_values = np.asarray(dataset[spec.lat_name].values, dtype=float)
    lon_values = np.asarray(dataset[spec.lon_name].values, dtype=float)
    source_west, source_east = _coord_range(lon_values)
    west, east = _target_longitudes_for_source(np.asarray([bbox.west, bbox.east]), lon_values)
    lat_min, lat_max = _coord_range(lat_values)
    if bbox.south < lat_min or bbox.north > lat_max:
        raise ValidationError(
            f"requested bbox latitude range [{bbox.south}, {bbox.north}] is outside source [{lat_min}, {lat_max}]"
        )
    if float(west) < source_west or float(east) > source_east:
        raise ValidationError(
            f"requested bbox longitude range [{bbox.west}, {bbox.east}] is outside source [{source_west}, {source_east}]"
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


def _coord_range(values: Any) -> tuple[float, float]:
    array = np.asarray(values, dtype=float)
    return float(np.nanmin(array)), float(np.nanmax(array))


def _time_range(values: Any) -> tuple[str, str]:
    array = np.asarray(values)
    if array.size == 0:
        return "", ""
    return str(array[0]), str(array[-1])


CopernicusNetCDFSource = NetCDFCurrentSource
