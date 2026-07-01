"""pyTMD/TPXO tidal-current source."""

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


def pytmd_is_available() -> bool:
    return importlib.util.find_spec("pyTMD") is not None


def _import_pytmd_compute():
    try:
        import pyTMD.compute as compute
    except ImportError as exc:
        raise MissingDependencyError(
            "pyTMD is not installed. Install TPXO support with "
            "`pip install tidal-current-grib-generator[tpxo]` and provide licensed model files."
        ) from exc
    return compute


@dataclass(frozen=True)
class SourceInspection:
    name: str
    model_directory: Path | None
    model_name: str | None
    definition_file: Path | None
    pytmd_available: bool
    model_directory_exists: bool | None
    definition_file_exists: bool | None
    current_prediction_available: bool
    constituents_u: list[str]
    constituents_v: list[str]
    details: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model_directory": str(self.model_directory) if self.model_directory else None,
            "model_name": self.model_name,
            "definition_file": str(self.definition_file) if self.definition_file else None,
            "pytmd_available": self.pytmd_available,
            "model_directory_exists": self.model_directory_exists,
            "definition_file_exists": self.definition_file_exists,
            "current_prediction_available": self.current_prediction_available,
            "constituents_u": self.constituents_u,
            "constituents_v": self.constituents_v,
            "details": self.details,
        }


@dataclass(frozen=True)
class PyTMDTPXOSource(CurrentSource):
    """TPXO/pyTMD source for astronomical tidal currents.

    This implementation uses the pyTMD v3 high-level API:
    ``pyTMD.compute.tide_currents(...)``. The official docs describe that
    function as returning a DataTree with ``u`` zonal and ``v`` meridional tidal
    currents in cm/s. We convert those velocity components to m/s for the
    project data model. We deliberately do not use transport fields directly,
    because transport-to-velocity conversion depends on depth and model details.
    """

    model_directory: Path
    model_name: str = "TPXO10-atlas-v2-nc"
    definition_file: Path | None = None
    interpolation_method: str = "linear"
    extrapolate: bool = False
    extrapolation_cutoff_km: float = 10.0
    crop_buffer_degrees: float = 1.0
    infer_minor: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_directory", self.model_directory.expanduser())
        if self.definition_file is not None:
            object.__setattr__(self, "definition_file", self.definition_file.expanduser())

    def describe(self) -> SourceDescription:
        model_label = self.definition_file.name if self.definition_file else self.model_name
        return SourceDescription(
            name="tpxo",
            summary=f"pyTMD tidal-current source using {model_label}.",
            data_notice="Astronomical tidal-current model; users supply model data under suitable terms.",
        )

    def validate_available(self) -> None:
        if not self.model_directory.exists():
            raise ValidationError(f"model directory does not exist: {self.model_directory}")
        if not self.model_directory.is_dir():
            raise ValidationError(f"model directory is not a directory: {self.model_directory}")
        if self.definition_file is not None and not self.definition_file.exists():
            raise ValidationError(f"model definition file does not exist: {self.definition_file}")
        _import_pytmd_compute()

    def get_current_grid(self, bbox: BoundingBox, time: datetime, grid: RegularGrid) -> CurrentGrid:
        return next(iter(self.get_current_grids(bbox, [time], grid)))

    def get_current_grids(self, bbox: BoundingBox, times: list[datetime], grid: RegularGrid) -> list[CurrentGrid]:
        self.validate_available()
        if not times:
            return []
        compute = _import_pytmd_compute()

        # pyTMD expects x=longitude, y=latitude in EPSG:4326 for crs=4326.
        # Use flattened point/drift mode instead of pyTMD grid mode. This
        # avoids pyTMD 3.x trying to create 2-D coordinate DataArrays from 1-D
        # project grid vectors, and keeps the return shape easy to validate.
        lon2d, lat2d = np.meshgrid(grid.longitudes, grid.latitudes)
        lon_points = lon2d.ravel()
        lat_points = lat2d.ravel()
        nt = len(times)
        npoints = lon_points.size
        valid_datetimes = [
            np.datetime64(t.astimezone(timezone.utc).replace(tzinfo=None), "ns")
            for t in times
        ]
        x = np.tile(lon_points, nt)
        y = np.tile(lat_points, nt)
        valid_time = np.repeat(np.asarray(valid_datetimes, dtype="datetime64[ns]"), npoints)
        result = compute.tide_currents(
            x,
            y,
            valid_time,
            directory=self.model_directory,
            model=None if self.definition_file else self.model_name,
            definition_file=self.definition_file,
            crs=4326,
            standard="datetime",
            type="drift",
            method=self.interpolation_method,
            extrapolate=self.extrapolate,
            cutoff=self.extrapolation_cutoff_km,
            infer_minor=self.infer_minor,
            chunks="auto",
            crop=True,
            buffer=self.crop_buffer_degrees,
            bounds=[bbox.west, bbox.east, bbox.south, bbox.north],
        )
        u_cm_s = _component_time_values(result, "u", (nt, grid.ny, grid.nx))
        v_cm_s = _component_time_values(result, "v", (nt, grid.ny, grid.nx))
        grids: list[CurrentGrid] = []
        for index, time in enumerate(times):
            u_mps = u_cm_s[index] / 100.0
            v_mps = v_cm_s[index] / 100.0
            mask = np.isnan(u_mps) | np.isnan(v_mps)
            grids.append(
                CurrentGrid(
                    time=time.astimezone(timezone.utc),
                    grid=grid,
                    u_mps=np.where(mask, 0.0, u_mps),
                    v_mps=np.where(mask, 0.0, v_mps),
                    mask=mask if mask.any() else None,
                )
            )
        return grids

    def inspect(self) -> dict[str, Any]:
        return inspect_pytmd_source(
            model_directory=self.model_directory,
            model_name=self.model_name,
            definition_file=self.definition_file,
        ).as_dict()


def inspect_pytmd_source(
    model_directory: Path | None,
    model_name: str | None,
    definition_file: Path | None = None,
) -> SourceInspection:
    details: list[str] = []
    directory_exists = None
    definition_exists = None
    if model_directory is not None:
        model_directory = model_directory.expanduser()
        directory_exists = model_directory.exists() and model_directory.is_dir()
        if not directory_exists:
            details.append(f"model directory not found: {model_directory}")
    if definition_file is not None:
        definition_file = definition_file.expanduser()
        definition_exists = definition_file.exists()
        if not definition_exists:
            details.append(f"definition file not found: {definition_file}")

    if not pytmd_is_available():
        details.append("pyTMD is not installed")
        return SourceInspection(
            name="tpxo",
            model_directory=model_directory,
            model_name=model_name,
            definition_file=definition_file,
            pytmd_available=False,
            model_directory_exists=directory_exists,
            definition_file_exists=definition_exists,
            current_prediction_available=False,
            constituents_u=[],
            constituents_v=[],
            details=details,
        )

    constituents_u: list[str] = []
    constituents_v: list[str] = []
    constituent_parse_errors: dict[str, str] = {}
    current_available = False
    try:
        import pyTMD.io

        model_factory = pyTMD.io.model(model_directory, verify=False)
        model = (
            model_factory.from_file(definition_file)
            if definition_file is not None
            else model_factory.from_database(model_name, group=("u", "v"))
        )
        current_available = hasattr(model, "u") and hasattr(model, "v")
        for group, target in (("u", constituents_u), ("v", constituents_v)):
            try:
                target.extend(str(c) for c in model.parse_constituents(group=group))
            except Exception as exc:  # pragma: no cover - depends on local model layout
                constituent_parse_errors[group] = str(exc)
        details.append(f"model format: {getattr(model, 'format', 'unknown')}")
        projection = getattr(model, "projection", None)
        if projection:
            details.append(f"projection: {projection}")
    except Exception as exc:  # pragma: no cover - depends on pyTMD/model version
        details.append(f"pyTMD model inspection failed: {exc}")

    if model_directory is not None:
        if not constituents_u:
            constituents_u = _scan_constituents_from_filenames(model_directory, "u")
            if constituents_u:
                details.append("u constituents discovered from filenames")
            elif "u" in constituent_parse_errors:
                details.append(f"could not parse u constituents: {constituent_parse_errors['u']}")
        if not constituents_v:
            constituents_v = _scan_constituents_from_filenames(model_directory, "v")
            if not constituents_v:
                constituents_v = _scan_constituents_from_filenames(model_directory, "h")
            if constituents_v:
                details.append("v constituents discovered from filenames")
            elif "v" in constituent_parse_errors:
                details.append(f"could not parse v constituents: {constituent_parse_errors['v']}")

    return SourceInspection(
        name="tpxo",
        model_directory=model_directory,
        model_name=model_name,
        definition_file=definition_file,
        pytmd_available=True,
        model_directory_exists=directory_exists,
        definition_file_exists=definition_exists,
        current_prediction_available=current_available,
        constituents_u=constituents_u,
        constituents_v=constituents_v,
        details=details,
    )


def _component_values(result: Any, component: str, expected_shape: tuple[int, int]) -> np.ndarray:
    return _component_array(result, component, expected_shape)


def _component_time_values(result: Any, component: str, expected_shape: tuple[int, int, int]) -> np.ndarray:
    return _component_array(result, component, expected_shape)


def _component_array(result: Any, component: str, expected_shape: tuple[int, ...]) -> np.ndarray:
    obj = _component_object(result, component)
    values = _values_from_xarray_like(obj, component)
    values = np.ma.filled(np.ma.asarray(values, dtype=np.float64), np.nan)
    values = np.squeeze(values)
    if values.shape == expected_shape:
        return values
    if len(expected_shape) == 2 and values.shape == (expected_shape[1], expected_shape[0]):
        # Some xarray/pyTMD paths expose dimensions as x,y. The project grid is
        # always y,x == latitude,longitude, so transpose this one clear case.
        return values.T
    if len(expected_shape) == 3 and values.shape == (expected_shape[0], expected_shape[1] * expected_shape[2]):
        return values.reshape(expected_shape)
    if len(expected_shape) == 3 and values.shape == (expected_shape[1] * expected_shape[2], expected_shape[0]):
        return values.T.reshape(expected_shape)
    expected_size = int(np.prod(expected_shape))
    if values.ndim == 1 and values.size == expected_size:
        # Point/drift mode returns values in the raveled meshgrid order
        # produced by numpy.meshgrid(lon, lat), which reshapes to y,x.
        return values.reshape(expected_shape)
    raise ValidationError(
        f"pyTMD returned {component!r} with shape {values.shape}, expected {expected_shape}; "
        "cannot safely reshape tidal-current output"
    )


def _component_object(result: Any, component: str) -> Any:
    if isinstance(result, dict) and component in result:
        return result[component]
    if isinstance(result, (tuple, list)):
        index = 0 if component == "u" else 1
        if len(result) > index:
            return result[index]
    if hasattr(result, "__getitem__"):
        try:
            return result[component]
        except Exception:
            pass
    if hasattr(result, component):
        return getattr(result, component)
    raise ValidationError(f"could not extract {component!r} component from pyTMD result")


def _values_from_xarray_like(obj: Any, component: str) -> Any:
    if isinstance(obj, (np.ndarray, np.ma.MaskedArray)):
        return obj
    if hasattr(obj, "values"):
        return obj.values
    if hasattr(obj, "to_dataset"):
        dataset = obj.to_dataset()
        if component in dataset:
            return dataset[component].values
        data_vars = list(getattr(dataset, "data_vars", []))
        if data_vars:
            return dataset[data_vars[0]].values
    if hasattr(obj, "ds"):
        dataset = obj.ds
        if component in dataset:
            return dataset[component].values
        data_vars = list(getattr(dataset, "data_vars", []))
        if data_vars:
            return dataset[data_vars[0]].values
    raise ValidationError(f"could not extract {component!r} values from pyTMD result")


def _scan_constituents_from_filenames(model_directory: Path, prefix: str) -> list[str]:
    constituents: set[str] = set()
    for path in model_directory.rglob(f"{prefix}_*.nc"):
        stem = path.stem.lower()
        if stem.startswith("grid_"):
            continue
        parts = stem.split("_")
        if len(parts) >= 2 and parts[1]:
            constituents.add(parts[1])
    return sorted(constituents)


PyTMDSource = PyTMDTPXOSource
