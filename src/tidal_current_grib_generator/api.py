"""Stable-ish library API for GUI/plugin callers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tidal_current_grib_generator.copernicus import (
    CopernicusDownloadRequest,
    CopernicusDownloadResult,
    download_copernicus_subset,
)
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, build_time_sequence
from tidal_current_grib_generator.grib.validation import inspect_grib, scan_grib_messages
from tidal_current_grib_generator.grib.writer import EccodesGrib1CurrentWriter
from tidal_current_grib_generator.sources import create_source
from tidal_current_grib_generator.sources.netcdf import NetCDFCurrentSource, inspect_netcdf

ProgressCallback = Callable[[str, dict[str, Any]], None]
CancellationHook = Callable[[], bool]


@dataclass(frozen=True)
class GenerateCurrentGribRequest:
    bbox: BoundingBox
    start: datetime
    hours: int
    step_hours: int
    output: Path
    source: str = "netcdf"
    grid_spacing_deg: float = 0.03
    input_netcdf: Path | None = None
    clip_bbox_to_source: bool = True
    use_source_grid: bool = True
    coverage_tolerance_deg: float = 0.02
    source_grid_regularity_tolerance: float = 1e-5

    def validate(self) -> None:
        self.bbox.validate()
        if self.hours < 0:
            raise ValueError("hours must be zero or greater")
        if self.step_hours <= 0:
            raise ValueError("step_hours must be greater than zero")
        if self.start.tzinfo is None:
            raise ValueError("start must be timezone-aware")


@dataclass(frozen=True)
class GenerateCurrentGribResult:
    output: Path
    message_count: int
    byte_count: int
    summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output),
            "message_count": self.message_count,
            "byte_count": self.byte_count,
            "summary": self.summary,
        }


def default_output_filename(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"current_grib_{now:%Y%m%d_%H%M}.grb"


def generate_current_grib_from_netcdf(
    request: GenerateCurrentGribRequest,
    progress_callback: ProgressCallback | None = None,
    cancellation_hook: CancellationHook | None = None,
) -> GenerateCurrentGribResult:
    request.validate()
    source = create_source(
        "netcdf",
        input_netcdf=request.input_netcdf,
        coverage_tolerance_deg=request.coverage_tolerance_deg,
        use_source_grid=request.use_source_grid,
        source_grid_regularity_tolerance=request.source_grid_regularity_tolerance,
    )
    bbox = request.bbox
    if request.clip_bbox_to_source and isinstance(source, NetCDFCurrentSource):
        bbox = source.clip_bbox_to_source(bbox)
    grid = (
        source.build_source_grid(bbox)
        if request.use_source_grid and isinstance(source, NetCDFCurrentSource)
        else build_regular_grid(bbox, request.grid_spacing_deg)
    )
    times = build_time_sequence(request.start.astimezone(timezone.utc), request.hours, request.step_hours)
    if progress_callback:
        progress_callback("generating GRIB", {"steps": len(times), "output": str(request.output)})

    def grids():
        for index, time in enumerate(times, start=1):
            if cancellation_hook and cancellation_hook():
                raise RuntimeError("generation cancelled")
            if progress_callback:
                progress_callback("generating timestep", {"index": index, "time": time.isoformat()})
            yield source.get_current_grid(bbox, time, grid)

    writer = EccodesGrib1CurrentWriter()
    write_summary = writer.write(grids(), request.output)
    scan = scan_grib_messages(write_summary.output)
    if progress_callback:
        progress_callback("validating GRIB", {"messages": scan.message_count})
    summary = {
        "source": "netcdf",
        "input_file": str(request.input_netcdf),
        "output_file": str(write_summary.output),
        "bbox": bbox.__dict__,
        "grid_size": {"nx": grid.nx, "ny": grid.ny, "points": grid.nx * grid.ny},
        "time_range": {"start": times[0].isoformat(), "end": times[-1].isoformat(), "step_count": len(times)},
        "message_count": write_summary.message_count,
        "bbox_clipped": bbox != request.bbox,
        "use_source_grid": request.use_source_grid,
    }
    return GenerateCurrentGribResult(
        output=write_summary.output,
        message_count=scan.message_count,
        byte_count=scan.byte_count,
        summary=summary,
    )


__all__ = [
    "BoundingBox",
    "CopernicusDownloadRequest",
    "CopernicusDownloadResult",
    "GenerateCurrentGribRequest",
    "GenerateCurrentGribResult",
    "download_copernicus_subset",
    "default_output_filename",
    "generate_current_grib_from_netcdf",
    "inspect_grib",
    "inspect_netcdf",
]
