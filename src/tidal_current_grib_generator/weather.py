"""Weather GRIB provider helpers."""

from __future__ import annotations

import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox
from tidal_current_grib_generator.grib.validation import inspect_grib, scan_grib_messages

GFS_FILTER_ENDPOINT = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
GFS_WAVE_FILTER_ENDPOINT = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfswave.pl"
GFS_SOURCE_LABEL = "NOAA GFS 0.25° forecast via NOMADS"
GFS_WAVE_SOURCE_LABEL = "NOAA GFS Wave forecast via NOMADS"
ECMWF_SOURCE_LABEL = "ECMWF IFS Open Data forecast"
GFS_ROUTING_VARIABLES_LEVELS = {
    "var_UGRD": "on",
    "var_VGRD": "on",
    "var_PRMSL": "on",
    "var_TMP": "on",
    "lev_10_m_above_ground": "on",
    "lev_mean_sea_level": "on",
    "lev_2_m_above_ground": "on",
}
GFS_MINIMAL_VARIABLES_LEVELS = {
    "var_UGRD": "on",
    "var_VGRD": "on",
    "lev_10_m_above_ground": "on",
}
GFS_MARINE_EXTRA_VARIABLES_LEVELS = {
    "var_GUST": "on",
    "var_TCDC": "on",
    "var_APCP": "on",
    "lev_surface": "on",
    "lev_entire_atmosphere": "on",
}
GFS_WAVE_VARIABLES_LEVELS = {
    "var_HTSGW": "on",
    "var_PERPW": "on",
    "var_DIRPW": "on",
    "lev_surface": "on",
}
GFS_VARIABLES_LEVELS = GFS_ROUTING_VARIABLES_LEVELS
ECMWF_PARAMETERS = ["10u", "10v", "msl", "2t"]
ECMWF_VARIABLES_LEVELS = {
    "param": ECMWF_PARAMETERS,
    "levtype": "sfc",
    "type": "fc",
}

HttpGet = Callable[[str, float], bytes]


class EcmwfClientFactory(Protocol):
    def __call__(self, **kwargs: Any) -> Any:
        ...


@dataclass(frozen=True)
class WeatherProvider:
    id: str
    label: str
    source: str
    format: str
    account: str
    description: str
    implemented: bool = True

    def as_dict(self) -> dict[str, Any]:
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
class WeatherCycle:
    date: str
    cycle: str

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
    preset: str = "routing"


@dataclass(frozen=True)
class ECMWFWeatherRequest:
    bbox: BoundingBox
    output: Path
    hours: int
    step_hours: int = 3
    cycle: str = "auto"
    date: str | None = None
    overwrite: bool = False
    timeout_seconds: float = 180.0
    dry_run: bool = False
    preset: str = "routing"


@dataclass(frozen=True)
class GFSWaveRequest:
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
    cycle: GFSCycle | WeatherCycle
    bbox: BoundingBox
    forecast_hours: list[int]
    output: Path
    byte_count: int
    message_count: int
    inspection: dict[str, Any]
    urls: list[str]
    variables_levels: dict[str, Any]
    warnings: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "source": self.source,
            "model": self.model,
            "cycle": self.cycle.cycle_time,
            "bbox": self.bbox.__dict__,
            "forecast_hours": self.forecast_hours,
            "variables_levels": self.variables_levels,
            "output": str(self.output),
            "byte_count": self.byte_count,
            "message_count": self.message_count,
            "inspection": self.inspection,
            "urls": self.urls,
            "warnings": self.warnings or [],
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
        ),
        WeatherProvider(
            id="gfs_wave",
            label="NOAA GFS Wave forecast",
            source="NOAA NOMADS",
            format="GRIB2",
            account="free/no account",
            description="GFS Wave global 0.25 degree GRIB2 subsets from the official NOMADS wave filter.",
        ),
        WeatherProvider(
            id="ecmwf_ifs_open",
            label="ECMWF IFS Open Data forecast",
            source="ECMWF Open Data",
            format="GRIB2",
            account="free/no account",
            description=(
                "ECMWF Integrated Forecasting System Open Data GRIB2 forecast. "
                "Initial implementation retrieves the requested fields without spatial cropping."
            ),
        ),
        WeatherProvider(
            id="dwd_icon_eu",
            label="DWD ICON-EU forecast",
            source="DWD Open Data",
            format="GRIB2",
            account="free/no account",
            description="Planned ICON-EU GRIB2 provider. Not implemented in this CLI build yet.",
            implemented=False,
        ),
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
    variables_levels = gfs_variables_for_preset(request.preset)
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
            urls=[build_gfs_filter_url(planned_cycle, hour, request.bbox, variables_levels=variables_levels) for hour in forecast_hours],
            variables_levels=variables_levels,
        )

    selected_cycle: GFSCycle | None = None
    urls: list[str] = []
    segments: list[tuple[int, str, bytes]] = []
    errors: list[str] = []

    for candidate in candidates:
        try:
            segments = _download_gfs_cycle_segments(
                candidate,
                forecast_hours,
                request.bbox,
                http_get,
                request.timeout_seconds,
                variables_levels=variables_levels,
                progress_callback=progress_callback,
                provider_label="GFS",
                url_builder=build_gfs_filter_url,
            )
            selected_cycle = candidate
            urls = [url for _, url, _ in segments]
            _progress(progress_callback, "selected GFS cycle", {"cycle": candidate.cycle_time})
            break
        except ValidationError as exc:
            errors.append(f"{candidate.cycle_time}: {exc}")
            if request.cycle != "auto":
                raise
            _progress(
                progress_callback,
                "GFS cycle incomplete",
                {"cycle": candidate.cycle_time, "error": str(exc)},
            )
            time.sleep(min(request.retry_delay_seconds, 5.0))
    if selected_cycle is None or not segments:
        raise ValidationError(
            "No complete GFS cycle was available for the requested hours. "
            "Try a shorter duration or explicit older cycle. Tried: " + "; ".join(errors)
        )

    tmp_path: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            for forecast_hour, _url, segment in segments:
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
        variables_levels=variables_levels,
    )


def generate_gfs_wave_grib(
    request: GFSWaveRequest,
    *,
    http_get: HttpGet | None = None,
    now: datetime | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> WeatherGenerateResult:
    request.bbox.validate()
    _validate_gfs_wave_request(request)
    http_get = http_get or _http_get
    output = request.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")
    if output.exists() and not request.overwrite:
        raise ValidationError(f"output already exists: {output}; use --overwrite to replace it")
    forecast_hours = forecast_hour_sequence(request.hours, request.step_hours)
    candidates = gfs_wave_cycle_candidates(request, now=now)
    if request.dry_run:
        planned_cycle = candidates[0]
        return WeatherGenerateResult(
            provider="gfs_wave",
            source=GFS_WAVE_SOURCE_LABEL,
            model="gfswave_global_0p25",
            cycle=planned_cycle,
            bbox=request.bbox,
            forecast_hours=forecast_hours,
            output=output,
            byte_count=0,
            message_count=0,
            inspection={"stream_valid": False, "message_count": 0, "dry_run": True},
            urls=[build_gfs_wave_filter_url(planned_cycle, hour, request.bbox) for hour in forecast_hours],
            variables_levels=GFS_WAVE_VARIABLES_LEVELS,
        )

    selected_cycle: GFSCycle | None = None
    urls: list[str] = []
    segments: list[tuple[int, str, bytes]] = []
    errors: list[str] = []
    for candidate in candidates:
        try:
            segments = _download_gfs_cycle_segments(
                candidate,
                forecast_hours,
                request.bbox,
                http_get,
                request.timeout_seconds,
                variables_levels=None,
                progress_callback=progress_callback,
                provider_label="GFS Wave",
                url_builder=build_gfs_wave_filter_url,
            )
            selected_cycle = candidate
            urls = [url for _, url, _ in segments]
            _progress(progress_callback, "selected GFS Wave cycle", {"cycle": candidate.cycle_time})
            break
        except ValidationError as exc:
            errors.append(f"{candidate.cycle_time}: {exc}")
            if request.cycle != "auto":
                raise
            _progress(
                progress_callback,
                "GFS Wave cycle incomplete",
                {"cycle": candidate.cycle_time, "error": str(exc)},
            )
            time.sleep(min(request.retry_delay_seconds, 5.0))
    if selected_cycle is None or not segments:
        raise ValidationError(
            "No complete GFS Wave cycle was available for the requested hours. "
            "Try a shorter duration or explicit older cycle. Tried: " + "; ".join(errors)
        )

    tmp_path: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            for forecast_hour, _url, segment in segments:
                tmp.write(segment)
                _progress(progress_callback, "downloaded GFS Wave forecast hour", {"cycle": selected_cycle.cycle_time, "hour": forecast_hour, "bytes": len(segment)})
        scan = scan_grib_messages(tmp_path)
        if scan.message_count <= 0:
            raise ValidationError("combined GFS Wave GRIB contains no messages")
        inspection = inspect_grib(tmp_path)
        tmp_path.replace(output)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()

    return WeatherGenerateResult(
        provider="gfs_wave",
        source=GFS_WAVE_SOURCE_LABEL,
        model="gfswave_global_0p25",
        cycle=selected_cycle,
        bbox=request.bbox,
        forecast_hours=forecast_hours,
        output=output,
        byte_count=scan.byte_count,
        message_count=scan.message_count,
        inspection=inspection,
        urls=urls,
        variables_levels=GFS_WAVE_VARIABLES_LEVELS,
    )


def generate_ecmwf_weather_grib(
    request: ECMWFWeatherRequest,
    *,
    client_factory: EcmwfClientFactory | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> WeatherGenerateResult:
    """Retrieve ECMWF IFS Open Data using the official ecmwf-opendata client.

    The official client handles the ECMWF Open Data index and byte-range
    retrieval. The public client API documents parameter/step/date/time
    selection, but not geographic subsetting, so this first provider accepts a
    bbox for workflow compatibility and metadata while retrieving the published
    global 0.25 degree fields.
    """

    request.bbox.validate()
    _validate_ecmwf_request(request)
    output = request.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")
    if output.exists() and not request.overwrite:
        raise ValidationError(f"output already exists: {output}; use --overwrite to replace it")
    forecast_hours = forecast_hour_sequence(request.hours, request.step_hours)
    warning = "ECMWF Open Data provider currently retrieves global fields; bbox is recorded but not spatially cropped"

    if request.cycle == "auto":
        planned_cycle = WeatherCycle("auto", "auto")
    else:
        if request.date is None:
            raise ValidationError("--date YYYYMMDD is required when --cycle is explicit")
        planned_cycle = WeatherCycle(request.date, request.cycle)

    if request.dry_run:
        return WeatherGenerateResult(
            provider="ecmwf_ifs_open",
            source=ECMWF_SOURCE_LABEL,
            model="ecmwf_ifs_open_0p25",
            cycle=planned_cycle,
            bbox=request.bbox,
            forecast_hours=forecast_hours,
            output=output,
            byte_count=0,
            message_count=0,
            inspection={"stream_valid": False, "message_count": 0, "dry_run": True},
            urls=[],
            variables_levels=ECMWF_VARIABLES_LEVELS,
            warnings=[warning],
        )

    client_factory = client_factory or _ecmwf_client_factory
    try:
        client = client_factory(source="ecmwf", model="ifs", resol="0p25")
    except MissingDependencyError:
        raise
    except Exception as exc:
        raise ValidationError(f"could not initialise ECMWF Open Data client: {exc}") from exc

    retrieve_request: dict[str, Any] = {
        "type": "fc",
        "step": forecast_hours,
        "param": ECMWF_PARAMETERS,
        "target": None,
    }
    if request.cycle != "auto":
        retrieve_request["date"] = request.date
        retrieve_request["time"] = int(request.cycle)

    tmp_path: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        retrieve_request["target"] = str(tmp_path)
        _progress(
            progress_callback,
            "retrieving ECMWF Open Data forecast",
            {"params": ECMWF_PARAMETERS, "forecast_hours": forecast_hours},
        )
        try:
            result = client.retrieve(**retrieve_request)
        except Exception as exc:
            raise ValidationError(f"ECMWF Open Data retrieval failed: {exc}") from exc
        if not tmp_path.exists() or tmp_path.stat().st_size == 0:
            raise ValidationError("ECMWF Open Data retrieval produced an empty GRIB file")
        _validate_downloaded_grib_file(tmp_path, "ECMWF Open Data")
        scan = scan_grib_messages(tmp_path)
        inspection = inspect_grib(tmp_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.replace(output)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()

    cycle_time = getattr(result, "datetime", None)
    selected_cycle = _weather_cycle_from_ecmwf_datetime(cycle_time) if cycle_time is not None else planned_cycle
    return WeatherGenerateResult(
        provider="ecmwf_ifs_open",
        source=ECMWF_SOURCE_LABEL,
        model="ecmwf_ifs_open_0p25",
        cycle=selected_cycle,
        bbox=request.bbox,
        forecast_hours=forecast_hours,
        output=output,
        byte_count=scan.byte_count,
        message_count=scan.message_count,
        inspection=inspection,
        urls=[],
        variables_levels=ECMWF_VARIABLES_LEVELS,
        warnings=[warning],
    )


def forecast_hour_sequence(hours: int, step_hours: int) -> list[int]:
    if hours < 0:
        raise ValidationError("--hours must be zero or greater")
    if step_hours <= 0:
        raise ValidationError("--step-hours must be greater than zero")
    if hours % step_hours != 0:
        raise ValidationError("--hours must be evenly divisible by --step-hours")
    return list(range(0, hours + 1, step_hours))


def gfs_variables_for_preset(preset: str) -> dict[str, str]:
    normalized = preset.strip().lower()
    if normalized == "minimal":
        return dict(GFS_MINIMAL_VARIABLES_LEVELS)
    if normalized == "routing":
        return dict(GFS_ROUTING_VARIABLES_LEVELS)
    if normalized == "marine":
        fields = dict(GFS_ROUTING_VARIABLES_LEVELS)
        fields.update(GFS_MARINE_EXTRA_VARIABLES_LEVELS)
        return fields
    raise ValidationError("--weather-preset must be minimal, routing, or marine")


def build_gfs_filter_url(
    cycle: GFSCycle,
    forecast_hour: int,
    bbox: BoundingBox,
    *,
    variables_levels: dict[str, str] | None = None,
) -> str:
    query = {
        "dir": cycle.directory,
        "file": f"gfs.t{cycle.cycle}z.pgrb2.0p25.f{forecast_hour:03d}",
        "subregion": "",
        "leftlon": f"{bbox.west:g}",
        "rightlon": f"{bbox.east:g}",
        "toplat": f"{bbox.north:g}",
        "bottomlat": f"{bbox.south:g}",
        **(variables_levels or GFS_VARIABLES_LEVELS),
    }
    return f"{GFS_FILTER_ENDPOINT}?{urlencode(query)}"


def build_gfs_wave_filter_url(cycle: GFSCycle, forecast_hour: int, bbox: BoundingBox) -> str:
    query = {
        "dir": f"/gfs.{cycle.date}/{cycle.cycle}/wave/gridded",
        "file": f"gfswave.t{cycle.cycle}z.global.0p25.f{forecast_hour:03d}.grib2",
        "subregion": "",
        "leftlon": f"{bbox.west:g}",
        "rightlon": f"{bbox.east:g}",
        "toplat": f"{bbox.north:g}",
        "bottomlat": f"{bbox.south:g}",
        **GFS_WAVE_VARIABLES_LEVELS,
    }
    return f"{GFS_WAVE_FILTER_ENDPOINT}?{urlencode(query)}"


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


def gfs_wave_cycle_candidates(request: GFSWaveRequest, *, now: datetime | None = None) -> list[GFSCycle]:
    shim = GFSWeatherRequest(
        bbox=request.bbox,
        output=request.output,
        hours=request.hours,
        step_hours=request.step_hours,
        cycle=request.cycle,
        date=request.date,
        overwrite=request.overwrite,
        timeout_seconds=request.timeout_seconds,
        retry_delay_seconds=request.retry_delay_seconds,
        max_auto_cycles=request.max_auto_cycles,
        dry_run=request.dry_run,
    )
    return gfs_cycle_candidates(shim, now=now)


def _download_gfs_cycle_segments(
    cycle: GFSCycle,
    forecast_hours: list[int],
    bbox: BoundingBox,
    http_get: HttpGet,
    timeout_seconds: float,
    *,
    variables_levels: dict[str, str] | None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None,
    provider_label: str,
    url_builder: Callable[..., str],
) -> list[tuple[int, str, bytes]]:
    segments: list[tuple[int, str, bytes]] = []
    for forecast_hour in forecast_hours:
        if provider_label == "GFS":
            url = url_builder(cycle, forecast_hour, bbox, variables_levels=variables_levels)
            download_step = "downloading GFS forecast hour"
        else:
            url = url_builder(cycle, forecast_hour, bbox)
            download_step = "downloading GFS Wave forecast hour"
        check_step = "checking GFS cycle" if provider_label == "GFS" else "checking GFS Wave cycle"
        _progress(progress_callback, check_step, {"cycle": cycle.cycle_time, "hour": forecast_hour})
        _progress(progress_callback, download_step, {"cycle": cycle.cycle_time, "hour": forecast_hour})
        try:
            data = _download_grib_segment(url, http_get, timeout_seconds, provider_label=provider_label)
        except ValidationError as exc:
            raise ValidationError(f"incomplete at f{forecast_hour:03d}: {exc}") from exc
        segments.append((forecast_hour, url, data))
    return segments


def _download_grib_segment(url: str, http_get: HttpGet, timeout_seconds: float, *, provider_label: str = "GFS") -> bytes:
    try:
        data = http_get(url, timeout_seconds)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ValidationError(f"{provider_label} download failed: {exc}") from exc
    _validate_downloaded_grib_bytes(data, provider_label=provider_label)
    return data


def _validate_downloaded_grib_bytes(data: bytes, *, provider_label: str = "GFS") -> None:
    if not data:
        raise ValidationError(f"{provider_label} download returned empty response")
    stripped = data.lstrip()
    if stripped.startswith((b"<", b"<!DOCTYPE", b"<html", b"<HTML")):
        raise ValidationError(f"{provider_label} download returned HTML/text instead of GRIB2")
    if b"GRIB" not in data[:32]:
        sample = stripped[:80].decode("utf-8", errors="replace")
        raise ValidationError(f"{provider_label} download did not start with a GRIB message: {sample!r}")
    with tempfile.NamedTemporaryFile(prefix="gfs-segment.", suffix=".grb2", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(data)
    try:
        scan = scan_grib_messages(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)
    if scan.message_count <= 0:
        raise ValidationError(f"{provider_label} download contained no GRIB messages")


def _validate_downloaded_grib_file(path: Path, provider_label: str) -> None:
    data = path.read_bytes()[:128]
    if not data:
        raise ValidationError(f"{provider_label} download returned empty response")
    stripped = data.lstrip()
    if stripped.startswith((b"<", b"<!DOCTYPE", b"<html", b"<HTML")):
        raise ValidationError(f"{provider_label} download returned HTML/text instead of GRIB2")
    if b"GRIB" not in data[:32]:
        sample = stripped[:80].decode("utf-8", errors="replace")
        raise ValidationError(f"{provider_label} download did not start with a GRIB message: {sample!r}")
    scan = scan_grib_messages(path)
    if scan.message_count <= 0:
        raise ValidationError(f"{provider_label} download contained no GRIB messages")


def _http_get(url: str, timeout_seconds: float) -> bytes:
    request = Request(url, headers={"User-Agent": "tidal-current-grib-generator/0"})
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def _validate_gfs_request(request: GFSWeatherRequest) -> None:
    if request.step_hours not in {1, 3, 6, 12}:
        raise ValidationError("--step-hours must be one of 1, 3, 6, or 12 for GFS")
    gfs_variables_for_preset(request.preset)
    forecast_hour_sequence(request.hours, request.step_hours)


def _validate_gfs_wave_request(request: GFSWaveRequest) -> None:
    if request.step_hours not in {1, 3, 6, 12}:
        raise ValidationError("--step-hours must be one of 1, 3, 6, or 12 for GFS Wave")
    forecast_hour_sequence(request.hours, request.step_hours)


def _validate_ecmwf_request(request: ECMWFWeatherRequest) -> None:
    if request.step_hours not in {3, 6, 12}:
        raise ValidationError("--step-hours must be one of 3, 6, or 12 for ECMWF Open Data")
    if request.cycle != "auto":
        if request.cycle not in {"00", "06", "12", "18"}:
            raise ValidationError("--cycle must be auto, 00, 06, 12, or 18")
        if not request.date:
            raise ValidationError("--date YYYYMMDD is required when --cycle is explicit")
        _validate_date(request.date)
    forecast_hour_sequence(request.hours, request.step_hours)


def _ecmwf_client_factory(**kwargs: Any) -> Any:
    try:
        from ecmwf.opendata import Client
    except ImportError as exc:
        raise MissingDependencyError(
            "ECMWF Open Data provider requires the optional ecmwf-opendata package; "
            "install with `pip install ecmwf-opendata` or `pip install tidal-current-grib-generator[weather]`."
        ) from exc
    return Client(**kwargs)


def _weather_cycle_from_ecmwf_datetime(value: Any) -> WeatherCycle:
    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return WeatherCycle(dt.strftime("%Y%m%d"), f"{dt.hour:02d}")
    text = str(value)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        dt = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return WeatherCycle(dt.strftime("%Y%m%d"), f"{dt.hour:02d}")
    except ValueError:
        return WeatherCycle(text, "")


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValidationError("--date must use YYYYMMDD format") from exc


def _progress(callback: Callable[[str, dict[str, Any]], None] | None, step: str, details: dict[str, Any]) -> None:
    if callback is not None:
        callback(step, details)
