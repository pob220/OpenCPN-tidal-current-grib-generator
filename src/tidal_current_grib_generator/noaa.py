"""NOAA current-provider discovery and conversion helpers."""

from __future__ import annotations

import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid
from tidal_current_grib_generator.grib.validation import scan_grib_messages
from tidal_current_grib_generator.grib.writer import EccodesGrib1CurrentWriter
from tidal_current_grib_generator.model import CurrentGrid

NOAA_RTOFS_NOMADS_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/rtofs/prod"
NOAA_RTOFS_AWS_BASE = "https://noaa-nws-rtofs-pds.s3.amazonaws.com"
NOAA_RTOFS_MAX_FORECAST_HOUR = 192
NOAA_RTOFS_DEFAULT_CYCLE = "00"
NOAA_RTOFS_STEP_HOURS = 6

RTOFS_REGION_COVERAGE = {
    "US_east": BoundingBox(-100.0, 0.0, -35.0, 55.0),
    "US_west": BoundingBox(-170.0, 10.0, -105.0, 65.0),
    "alaska": BoundingBox(-180.0, 45.0, -120.0, 75.0),
}


@dataclass(frozen=True)
class RTOFSCycle:
    date: str
    cycle: str = NOAA_RTOFS_DEFAULT_CYCLE

    @property
    def cycle_time(self) -> datetime:
        return datetime.strptime(self.date + self.cycle, "%Y%m%d%H").replace(tzinfo=timezone.utc)

    @property
    def directory_name(self) -> str:
        return f"rtofs.{self.date}"


@dataclass(frozen=True)
class RTOFSCurrentResult:
    output: Path
    message_count: int
    byte_count: int
    selected_cycle: str
    forecast_hours: list[int]
    source_files: list[Path]
    summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "output": str(self.output),
            "message_count": self.message_count,
            "byte_count": self.byte_count,
            "selected_cycle": self.selected_cycle,
            "forecast_hours": self.forecast_hours,
            "source_files": [str(path) for path in self.source_files],
            "summary": self.summary,
        }


ProgressCallback = Callable[[str, dict[str, Any]], None]


def rtofs_cycle_candidates(now: datetime | None = None, days: int = 5) -> list[RTOFSCycle]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = now.date()
    return [
        RTOFSCycle((today - timedelta(days=offset)).strftime("%Y%m%d"))
        for offset in range(max(1, days))
    ]


def rtofs_forecast_hours(requested_hours: int, step_hours: int) -> list[int]:
    if requested_hours <= 0:
        raise ValidationError("--hours must be greater than zero for NOAA RTOFS")
    if step_hours <= 0:
        raise ValidationError("--step-hours must be greater than zero for NOAA RTOFS")
    if requested_hours > NOAA_RTOFS_MAX_FORECAST_HOUR:
        raise ValidationError(
            f"NOAA RTOFS forecast currents are limited to {NOAA_RTOFS_MAX_FORECAST_HOUR} hours in this provider"
        )
    effective_step = max(NOAA_RTOFS_STEP_HOURS, step_hours)
    first = NOAA_RTOFS_STEP_HOURS
    hours = list(range(first, requested_hours + 1, effective_step))
    if not hours or hours[-1] != requested_hours:
        rounded = requested_hours - (requested_hours % effective_step)
        if rounded >= first and rounded not in hours:
            hours.append(rounded)
    return sorted(set(hour for hour in hours if first <= hour <= NOAA_RTOFS_MAX_FORECAST_HOUR))


def parse_rtofs_inventory(html: str) -> dict[int, list[str]]:
    pattern = re.compile(r"rtofs_glo_3dz_f(\d{3})_6hrly_hvr_([A-Za-z_]+)\.nc")
    result: dict[int, list[str]] = {}
    for match in pattern.finditer(html):
        hour = int(match.group(1))
        region = match.group(2)
        result.setdefault(hour, [])
        if region not in result[hour]:
            result[hour].append(region)
    return result


def rtofs_region_for_bbox(bbox: BoundingBox) -> str:
    for region, coverage in RTOFS_REGION_COVERAGE.items():
        if _bbox_contains(coverage, bbox):
            return region
    raise ValidationError(
        "NOAA RTOFS generation currently supports the public regional high-value NetCDF files "
        "for U.S. East, U.S. West, and Alaska domains. The requested bbox is outside those domains. "
        "Use Copernicus Global currents for other ocean areas in this build."
    )


def rtofs_nomads_directory_url(cycle: RTOFSCycle) -> str:
    return f"{NOAA_RTOFS_NOMADS_BASE}/{cycle.directory_name}/"


def rtofs_hvr_filename(hour: int, region: str) -> str:
    return f"rtofs_glo_3dz_f{hour:03d}_6hrly_hvr_{region}.nc"


def rtofs_hvr_url(cycle: RTOFSCycle, hour: int, region: str) -> str:
    return rtofs_nomads_directory_url(cycle) + rtofs_hvr_filename(hour, region)


def discover_rtofs_cycle(
    *,
    requested_hours: list[int],
    region: str,
    cycle: str = "auto",
    date: str | None = None,
    opener: Callable[[str], str] | None = None,
) -> RTOFSCycle:
    if cycle != "auto" and date is None:
        raise ValidationError("--date YYYYMMDD is required when NOAA RTOFS --cycle is explicit")
    candidates = [RTOFSCycle(date or "", cycle)] if cycle != "auto" else rtofs_cycle_candidates()
    read_text = opener or _read_url_text
    errors: list[str] = []
    for candidate in candidates:
        try:
            inventory = parse_rtofs_inventory(read_text(rtofs_nomads_directory_url(candidate)))
        except ValidationError as exc:
            errors.append(f"{candidate.directory_name}: {exc}")
            continue
        missing = [
            hour
            for hour in requested_hours
            if hour not in inventory or region not in inventory[hour]
        ]
        if not missing:
            return candidate
        errors.append(f"{candidate.directory_name}: missing {region} forecast hours {missing}")
    raise ValidationError(
        "No complete NOAA RTOFS cycle was available for the requested current forecast hours. "
        "Try a shorter duration or an explicit older cycle. Tried: " + "; ".join(errors)
    )


def generate_noaa_rtofs_current_grib(
    *,
    bbox: BoundingBox,
    output: Path,
    hours: int,
    step_hours: int,
    cycle: str,
    date: str | None,
    download_directory: Path,
    grid_spacing_deg: float,
    overwrite: bool,
    dry_run: bool = False,
    metadata_summary: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> RTOFSCurrentResult:
    output = output.expanduser()
    if output.exists() and not overwrite:
        raise ValidationError(f"output already exists: {output}; use --overwrite to replace it")
    forecast_hours = rtofs_forecast_hours(hours, step_hours)
    region = rtofs_region_for_bbox(bbox)
    selected_cycle = discover_rtofs_cycle(
        requested_hours=forecast_hours,
        region=region,
        cycle=cycle,
        date=date,
    )
    download_directory = download_directory.expanduser()
    source_files = [
        download_directory / selected_cycle.directory_name / rtofs_hvr_filename(hour, region)
        for hour in forecast_hours
    ]
    if dry_run:
        return RTOFSCurrentResult(
            output=output,
            message_count=0,
            byte_count=0,
            selected_cycle=selected_cycle.cycle_time.isoformat().replace("+00:00", "Z"),
            forecast_hours=forecast_hours,
            source_files=source_files,
            summary={
                "provider": "noaa_rtofs_global",
                "region": region,
                "source": "NOAA/NCEP RTOFS high-value regional NetCDF",
                "units": "m/s",
                "dry_run": True,
            },
        )
    for hour, path in zip(forecast_hours, source_files):
        url = rtofs_hvr_url(selected_cycle, hour, region)
        _download_file(url, path, progress_callback)

    grid = build_regular_grid(bbox, grid_spacing_deg)
    writer = EccodesGrib1CurrentWriter()
    write_summary = writer.write(
        _rtofs_current_grids(source_files, selected_cycle, forecast_hours, bbox, grid, progress_callback),
        output,
    )
    scan = scan_grib_messages(write_summary.output)
    summary = {
        "source": "noaa_rtofs_global",
        "output_file": str(write_summary.output),
        "bbox": bbox.__dict__,
        "grid_size": {"nx": grid.nx, "ny": grid.ny, "points": grid.nx * grid.ny},
        "time_range": {
            "start": (selected_cycle.cycle_time + timedelta(hours=forecast_hours[0])).isoformat(),
            "end": (selected_cycle.cycle_time + timedelta(hours=forecast_hours[-1])).isoformat(),
            "step_count": len(forecast_hours),
        },
        "message_count": write_summary.message_count,
        "regridding": "scipy linear interpolation from RTOFS curvilinear Y/X grid to requested regular lon/lat grid",
    }
    summary.update(
        {
            "provider": "noaa_rtofs_global",
            "region": region,
            "selected_cycle": selected_cycle.cycle_time.isoformat().replace("+00:00", "Z"),
            "forecast_hours": forecast_hours,
            "source_fields": ["u", "v"],
            "units": "m/s",
            "source": "NOAA/NCEP RTOFS high-value regional NetCDF",
            "source_urls": [rtofs_hvr_url(selected_cycle, hour, region) for hour in forecast_hours],
        }
    )
    if metadata_summary:
        summary["note"] = "RTOFS files were converted without changing units; output GRIB uses current parameters 49/50."
    return RTOFSCurrentResult(
        output=write_summary.output,
        message_count=scan.message_count,
        byte_count=scan.byte_count,
        selected_cycle=selected_cycle.cycle_time.isoformat().replace("+00:00", "Z"),
        forecast_hours=forecast_hours,
        source_files=source_files,
        summary=summary,
    )


def _read_url_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValidationError(f"could not read NOAA RTOFS inventory {url}: {exc}") from exc


def _download_file(url: str, path: Path, progress_callback: ProgressCallback | None) -> None:
    if path.exists() and path.stat().st_size > 0:
        if progress_callback:
            progress_callback("reusing NOAA RTOFS file", {"path": str(path), "bytes": path.stat().st_size})
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        if progress_callback:
            progress_callback("downloading NOAA RTOFS file", {"url": url, "path": str(path)})
        with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        tmp.replace(path)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise ValidationError(f"could not download NOAA RTOFS file {url}: {exc}") from exc


def _rtofs_current_grids(
    files: list[Path],
    cycle: RTOFSCycle,
    forecast_hours: list[int],
    bbox: BoundingBox,
    grid: Any,
    progress_callback: ProgressCallback | None,
):
    try:
        import numpy as np
        from scipy.interpolate import griddata
        import xarray as xr
    except ImportError as exc:
        raise MissingDependencyError("NOAA RTOFS NetCDF conversion requires xarray, numpy, and scipy.") from exc

    target_lon, target_lat = np.meshgrid(grid.longitudes, grid.latitudes)
    margin = max(0.5, float(grid.spacing_deg) * 3.0)
    for path, hour in zip(files, forecast_hours):
        valid_time = cycle.cycle_time + timedelta(hours=hour)
        if progress_callback:
            progress_callback("regridding NOAA RTOFS current", {"path": str(path), "forecast_hour": hour})
        with xr.open_dataset(path) as dataset:
            if "u" not in dataset or "v" not in dataset:
                raise ValidationError("NOAA RTOFS NetCDF file does not contain u/v current variables")
            if "Latitude" not in dataset or "Longitude" not in dataset:
                raise ValidationError("NOAA RTOFS NetCDF file does not contain Latitude/Longitude coordinates")
            lon = np.asarray(dataset["Longitude"].values, dtype=float)
            lat = np.asarray(dataset["Latitude"].values, dtype=float)
            subset = (
                (lon >= bbox.west - margin)
                & (lon <= bbox.east + margin)
                & (lat >= bbox.south - margin)
                & (lat <= bbox.north + margin)
            )
            if not np.any(subset):
                raise ValidationError("requested bbox does not overlap the selected NOAA RTOFS source file")
            u = np.asarray(dataset["u"].isel(MT=0, Depth=0).values, dtype=float)
            v = np.asarray(dataset["v"].isel(MT=0, Depth=0).values, dtype=float)
            points = np.column_stack((lon[subset].ravel(), lat[subset].ravel()))
            u_values = u[subset].ravel()
            v_values = v[subset].ravel()
            valid = np.isfinite(points[:, 0]) & np.isfinite(points[:, 1]) & np.isfinite(u_values) & np.isfinite(v_values)
            if np.count_nonzero(valid) < 3:
                raise ValidationError("NOAA RTOFS source subset does not contain enough valid current points")
            target_points = (target_lon, target_lat)
            u_interp = griddata(points[valid], u_values[valid], target_points, method="linear")
            v_interp = griddata(points[valid], v_values[valid], target_points, method="linear")
            missing = ~np.isfinite(u_interp) | ~np.isfinite(v_interp)
            if np.any(missing):
                u_nearest = griddata(points[valid], u_values[valid], target_points, method="nearest")
                v_nearest = griddata(points[valid], v_values[valid], target_points, method="nearest")
                u_interp = np.where(np.isfinite(u_interp), u_interp, u_nearest)
                v_interp = np.where(np.isfinite(v_interp), v_interp, v_nearest)
            mask = ~np.isfinite(u_interp) | ~np.isfinite(v_interp)
            yield CurrentGrid(
                time=valid_time,
                grid=grid,
                u_mps=np.where(mask, 0.0, u_interp),
                v_mps=np.where(mask, 0.0, v_interp),
                mask=mask if np.any(mask) else None,
            )


def _bbox_contains(coverage: BoundingBox, bbox: BoundingBox) -> bool:
    return (
        bbox.west >= coverage.west
        and bbox.east <= coverage.east
        and bbox.south >= coverage.south
        and bbox.north <= coverage.north
    )


__all__ = [
    "NOAA_RTOFS_AWS_BASE",
    "NOAA_RTOFS_NOMADS_BASE",
    "RTOFSCurrentResult",
    "RTOFSCycle",
    "discover_rtofs_cycle",
    "generate_noaa_rtofs_current_grib",
    "parse_rtofs_inventory",
    "rtofs_forecast_hours",
    "rtofs_hvr_url",
    "rtofs_region_for_bbox",
]
