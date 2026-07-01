"""Copernicus Marine download helpers."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Any

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox
from tidal_current_grib_generator.providers import COPERNICUS_NWS
from tidal_current_grib_generator.security import redact_mapping, redact_object

ProgressCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class CopernicusDownloadRequest:
    bbox: BoundingBox
    start: datetime
    end: datetime
    output_directory: Path
    output_filename: str
    username: str
    password: str
    dataset_id: str = COPERNICUS_NWS.dataset_id or ""
    variables: tuple[str, ...] = COPERNICUS_NWS.variables
    minimum_depth: float | None = None
    maximum_depth: float | None = None
    overwrite: bool = False
    dry_run: bool = False

    def validate(self) -> None:
        self.bbox.validate()
        if self.end <= self.start:
            raise ValidationError("Copernicus download end time must be after start time")
        if not self.username:
            raise ValidationError("Copernicus username is required")
        if not self.password:
            raise ValidationError("Copernicus password is required")
        if not self.dataset_id:
            raise ValidationError("Copernicus dataset id is required")
        if not self.output_filename.endswith(".nc"):
            raise ValidationError("Copernicus output filename should end with .nc")

    def safe_summary(self) -> dict[str, Any]:
        return redact_mapping(
            {
                "bbox": self.bbox.__dict__,
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "output_directory": str(self.output_directory),
                "output_filename": self.output_filename,
                "username": self.username,
                "password": self.password,
                "dataset_id": self.dataset_id,
                "variables": list(self.variables),
                "minimum_depth": self.minimum_depth,
                "maximum_depth": self.maximum_depth,
                "overwrite": self.overwrite,
                "dry_run": self.dry_run,
            }
        )


@dataclass(frozen=True)
class CopernicusDownloadResult:
    path: Path
    dataset_id: str
    variables: tuple[str, ...]
    dry_run: bool
    response: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "dataset_id": self.dataset_id,
            "variables": list(self.variables),
            "dry_run": self.dry_run,
            "response": self.response,
        }


def copernicusmarine_available() -> bool:
    return importlib.util.find_spec("copernicusmarine") is not None


def download_copernicus_subset(
    request: CopernicusDownloadRequest,
    progress_callback: ProgressCallback | None = None,
) -> CopernicusDownloadResult:
    request.validate()
    output_dir = request.output_directory.expanduser()
    output_path = output_dir / request.output_filename
    if progress_callback:
        progress_callback("downloading NetCDF", request.safe_summary())
    if not copernicusmarine_available():
        raise MissingDependencyError(
            "Copernicus downloads require the copernicusmarine package. "
            "Install `tidal-current-grib-generator[copernicus]`."
        )

    import copernicusmarine

    output_dir.mkdir(parents=True, exist_ok=True)
    response = copernicusmarine.subset(
        dataset_id=request.dataset_id,
        username=request.username,
        password=request.password,
        variables=list(request.variables),
        minimum_longitude=request.bbox.west,
        maximum_longitude=request.bbox.east,
        minimum_latitude=request.bbox.south,
        maximum_latitude=request.bbox.north,
        start_datetime=request.start,
        end_datetime=request.end,
        minimum_depth=request.minimum_depth,
        maximum_depth=request.maximum_depth,
        output_directory=output_dir,
        output_filename=request.output_filename,
        overwrite=request.overwrite,
        dry_run=request.dry_run,
        disable_progress_bar=True,
    )
    if progress_callback:
        progress_callback("download complete", {"path": str(output_path), "dry_run": request.dry_run})
    response_dict = response.model_dump(mode="json") if hasattr(response, "model_dump") else {"repr": repr(response)}
    response_dict = redact_object(response_dict)
    return CopernicusDownloadResult(
        path=output_path,
        dataset_id=request.dataset_id,
        variables=request.variables,
        dry_run=request.dry_run,
        response=response_dict,
    )
