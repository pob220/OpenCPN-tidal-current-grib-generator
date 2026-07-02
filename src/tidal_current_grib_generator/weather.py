"""Weather GRIB provider helpers."""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tidal_current_grib_generator.errors import ValidationError
from tidal_current_grib_generator.geo import BoundingBox
from tidal_current_grib_generator.grib.validation import inspect_grib, scan_grib_messages

GFS_FILTER_ENDPOINT = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
GFS_SOURCE_LABEL = "NOAA GFS 0.25° forecast via NOMADS"
GFS_VARIABLES_LEVELS = {
    "var_UGRD": "on",
    "var_VGRD": "on",
    "var_PRMSL": "on",
    "var_TMP": "on",
    "lev_10_m_above_ground": "on",
    "lev_mean_sea_level": "on",
    "lev_2_m_above_ground": "on",
}

HttpGet = Callable[[str, float], bytes]


@dataclass(frozen=True)
class WeatherProvider:
    id: str
    label: str
    source: str
    format: str
    account: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class GFSCycle:
    date: str
    cycle: str

    @property
    def directory(self) -> str:
        return f"/gfs.{self.date}/{self.cycle}/atmos"

    @property
    def cycle_time(self) -> str:
        return f"{self.date}T{self.cycle}00Z"


@dataclass(frozen=True)
class GFSWeatherRequest:
    bbox: BoundingBox
    output: Path
    hours: int
    step_hours: int = 3
    cycle: str = "auto"
    date: str | None = None
    overwrite: bool = False
    timeout_seconds: float = 60.0
    retry_delay_seconds: float = 1.0
    max_auto_cycles: int = 8
    dry_run: bool = False


@dataclass(frozen=True)
class WeatherGenerateResult:
    provider: str
    source: str
    model: str
    cycle: GFSCycle
    bbox: BoundingBox
    forecast_hours: list[int]
    output: Path
    byte_count: int
    message_count: int
    inspection: dict[str, Any]
    urls: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "model": self.model,
            "cycle": self.cycle.cycle_time,
            "bbox": self.bbox.__dict__,
            "forecast_hours": self.forecast_hours,
            "variables_levels": GFS_VARIABLES_LEVELS,
            "output": str(self.output),
            "byte_count": self.byte_count,
            "message_count": self.message_count,
            "inspection": self.inspection,
            "urls": self.urls,
        }


def list_weather_providers() -> list[WeatherProvider]:
    return [
        WeatherProvider(
            id="gfs",
            label="NOAA GFS 0.25 degree global forecast",
            source="NOAA NOMADS",
            format="GRIB2",
            account="free/no account",
            description="Global Forecast System 0.25 degree GRIB2 subsets from the official NOMADS filter.",
        )
    ]


def generate_gfs_weather_grib(
    request: GFSWeatherRequest,
    *,
    http_get: HttpGet | None = None,
    now: datetime | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> WeatherGenerateResult:
    request.bbox.validate()
    _validate_gfs_request(request)
    http_get = http_get or _http_get
    output = request.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")
    if output.exists() and not request.overwrite:
        raise ValidationError(f"output already exists: {output}; use --overwrite to replace it")
    forecast_hours = forecast_hour_sequence(request.hours, request.step_hours)
    candidates = gfs_cycle_candidates(request, now=now)
    if request.dry_run:
        planned_cycle = candidates[0]
        inspection = {
            "stream_valid": False,
            "message_count": 0,
            "dry_run": True,
        }
        return WeatherGenerateResult(
            provider="gfs",
            source=GFS_SOURCE_LABEL,
            model="gfs_0p25",
            cycle=planned_cycle,
            bbox=request.bbox,
            forecast_hours=forecast_hours,
            output=output,
            byte_count=0,
            message_count=0,
            inspection=inspection,
            urls=[build_gfs_filter_url(planned_cycle, hour, request.bbox) for hour in forecast_hours],
        )

    first_bytes: bytes | None = None
    selected_cycle: GFSCycle | None = None
    urls: list[str] = []
    errors: list[str] = []

    for candidate in candidates:
        first_url = build_gfs_filter_url(candidate, forecast_hours[0], request.bbox)
        try:
            _progress(progress_callback, "checking GFS cycle", {"cycle": candidate.cycle_time, "hour": forecast_hours[0]})
            first_bytes = _download_grib_segment(first_url, http_get, request.timeout_seconds)
            selected_cycle = candidate
            urls.append(first_url)
            break
        except ValidationError as exc:
            errors.append(f"{candidate.cycle_time}: {exc}")
            if request.cycle != "auto":
                raise
            time.sleep(min(request.retry_delay_seconds, 5.0))
    if selected_cycle is None or first_bytes is None:
        raise ValidationError("no usable GFS cycle found; tried " + "; ".join(errors))

    tmp_path: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            _progress(progress_callback, "downloaded GFS forecast hour", {"cycle": selected_cycle.cycle_time, "hour": forecast_hours[0], "bytes": len(first_bytes)})
            tmp.write(first_bytes)
            for forecast_hour in forecast_hours[1:]:
                url = build_gfs_filter_url(selected_cycle, forecast_hour, request.bbox)
                _progress(progress_callback, "downloading GFS forecast hour", {"cycle": selected_cycle.cycle_time, "hour": forecast_hour})
                segment = _download_grib_segment(url, http_get, request.timeout_seconds)
                urls.append(url)
                tmp.write(segment)
                _progress(progress_callback, "downloaded GFS forecast hour", {"cycle": selected_cycle.cycle_time, "hour": forecast_hour, "bytes": len(segment)})
        scan = scan_grib_messages(tmp_path)
        if scan.message_count <= 0:
            raise ValidationError("combined GFS GRIB contains no messages")
        inspection = inspect_grib(tmp_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.replace(output)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()

    return WeatherGenerateResult(
        provider="gfs",
        source=GFS_SOURCE_LABEL,
        model="gfs_0p25",
        cycle=selected_cycle,
        bbox=request.bbox,
        forecast_hours=forecast_hours,
        output=output,
        byte_count=scan.byte_count,
        message_count=scan.message_count,
        inspection=inspection,
        urls=urls,
    )


def forecast_hour_sequence(hours: int, step_hours: int) -> list[int]:
    if hours < 0:
        raise ValidationError("--hours must be zero or greater")
    if step_hours <= 0:
        raise ValidationError("--step-hours must be greater than zero")
    if hours % step_hours != 0:
        raise ValidationError("--hours must be evenly divisible by --step-hours")
    return list(range(0, hours + 1, step_hours))


def build_gfs_filter_url(cycle: GFSCycle, forecast_hour: int, bbox: BoundingBox) -> str:
    query = {
        "dir": cycle.directory,
        "file": f"gfs.t{cycle.cycle}z.pgrb2.0p25.f{forecast_hour:03d}",
        "subregion": "",
        "leftlon": f"{bbox.west:g}",
        "rightlon": f"{bbox.east:g}",
        "toplat": f"{bbox.north:g}",
        "bottomlat": f"{bbox.south:g}",
        **GFS_VARIABLES_LEVELS,
    }
    return f"{GFS_FILTER_ENDPOINT}?{urlencode(query)}"


def gfs_cycle_candidates(request: GFSWeatherRequest, *, now: datetime | None = None) -> list[GFSCycle]:
    if request.cycle != "auto":
        if request.cycle not in {"00", "06", "12", "18"}:
            raise ValidationError("--cycle must be auto, 00, 06, 12, or 18")
        if not request.date:
            raise ValidationError("--date YYYYMMDD is required when --cycle is explicit")
        _validate_date(request.date)
        return [GFSCycle(request.date, request.cycle)]

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cycles: list[GFSCycle] = []
    base_hour = (now.hour // 6) * 6
    cursor = now.replace(hour=base_hour, minute=0, second=0, microsecond=0)
    # NOMADS publication can lag the nominal cycle; try recent cycles newest to
    # older and let actual GRIB availability select the usable run.
    for _ in range(max(1, request.max_auto_cycles)):
        cycles.append(GFSCycle(cursor.strftime("%Y%m%d"), f"{cursor.hour:02d}"))
        cursor -= timedelta(hours=6)
    return cycles


def _download_grib_segment(url: str, http_get: HttpGet, timeout_seconds: float) -> bytes:
    try:
        data = http_get(url, timeout_seconds)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ValidationError(f"GFS download failed: {exc}") from exc
    _validate_downloaded_grib_bytes(data)
    return data


def _validate_downloaded_grib_bytes(data: bytes) -> None:
    if not data:
        raise ValidationError("GFS download returned empty response")
    stripped = data.lstrip()
    if stripped.startswith((b"<", b"<!DOCTYPE", b"<html", b"<HTML")):
        raise ValidationError("GFS download returned HTML/text instead of GRIB2")
    if b"GRIB" not in data[:32]:
        sample = stripped[:80].decode("utf-8", errors="replace")
        raise ValidationError(f"GFS download did not start with a GRIB message: {sample!r}")
    with tempfile.NamedTemporaryFile(prefix="gfs-segment.", suffix=".grb2", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(data)
    try:
        scan = scan_grib_messages(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if scan.message_count <= 0:
        raise ValidationError("GFS download contained no GRIB messages")


def _http_get(url: str, timeout_seconds: float) -> bytes:
    request = Request(url, headers={"User-Agent": "tidal-current-grib-generator/0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def _validate_gfs_request(request: GFSWeatherRequest) -> None:
    if request.step_hours not in {1, 3, 6, 12}:
        raise ValidationError("--step-hours must be one of 1, 3, 6, or 12 for GFS")
    forecast_hour_sequence(request.hours, request.step_hours)


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValidationError("--date must use YYYYMMDD format") from exc


def _progress(callback: Callable[[str, dict[str, Any]], None] | None, step: str, details: dict[str, Any]) -> None:
    if callback is not None:
        callback(step, details)
