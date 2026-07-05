"""Weather GRIB provider helpers."""

from __future__ import annotations

import tempfile
import time
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from tidal_current_grib_generator.copernicus import CopernicusDownloadRequest, download_copernicus_subset
from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid
from tidal_current_grib_generator.grib.validation import inspect_grib, scan_grib_messages

GFS_FILTER_ENDPOINT = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
GFS_WAVE_FILTER_ENDPOINT = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfswave.pl"
GFS_SOURCE_LABEL = "NOAA GFS 0.25° forecast via NOMADS"
GFS_WAVE_SOURCE_LABEL = "NOAA GFS Wave forecast via NOMADS"
COPERNICUS_GLOBAL_WAVE_SOURCE_LABEL = "Copernicus Marine Global Waves forecast"
ECMWF_SOURCE_LABEL = "ECMWF IFS Open Data forecast"
UKMO_UKV_SOURCE_LABEL = "Met Office UKV 2 km forecast"
COPERNICUS_GLOBAL_WAVE_DATASET_ID = "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i"
COPERNICUS_GLOBAL_WAVE_PRODUCT_ID = "GLOBAL_ANALYSISFORECAST_WAV_001_027"
COPERNICUS_GLOBAL_WAVE_VARIABLES = ("VHM0", "VTPK", "VMDR")
COPERNICUS_GLOBAL_WAVE_MIN_VALID_CELLS = 1
COPERNICUS_GLOBAL_WAVE_ALIASES = {
    "swh": ("VHM0", "significant_wave_height", "sea_surface_wave_significant_height"),
    "perpw": ("VTPK", "peak_wave_period", "sea_surface_wave_peak_period"),
    "dirpw": ("VMDR", "mean_wave_direction", "sea_surface_wave_from_direction"),
}
UKMO_UKV_DOMAIN = BoundingBox(west=-12.0, south=48.0, east=4.0, north=62.0)
UKMO_UKV_BUCKET = "met-office-atmospheric-model-data"
UKMO_UKV_REGION = "eu-west-2"
UKMO_UKV_S3_ENDPOINT = f"https://{UKMO_UKV_BUCKET}.s3.{UKMO_UKV_REGION}.amazonaws.com/"
UKMO_UKV_REQUIRED_SOURCE_FIELDS = {
    "pressure_msl": {
        "filename_token": "pressure_at_mean_sea_level",
        "intended_grib_short_name": "prmsl/msl",
        "confidence": "medium",
    },
    "temperature_screen": {
        "filename_token": "temperature_at_screen_level",
        "intended_grib_short_name": "2t",
        "confidence": "medium",
    },
    "wind_speed_10m": {
        "filename_token": "wind_speed_at_10m",
        "intended_grib_short_name": "10si -> 10u/10v after direction conversion",
        "confidence": "blocked-pending-direction-convention",
    },
    "wind_direction_10m": {
        "filename_token": "wind_direction_at_10m",
        "intended_grib_short_name": "10wdir -> 10u/10v after direction conversion",
        "confidence": "blocked-pending-direction-convention",
    },
}
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
class UKMOUKVWeatherRequest:
    bbox: BoundingBox
    output: Path
    hours: int
    step_hours: int = 1
    cycle: str = "auto"
    date: str | None = None
    overwrite: bool = False
    timeout_seconds: float = 180.0
    dry_run: bool = False
    preset: str = "routing"
    weather_grid_spacing_deg: float = 0.025


@dataclass(frozen=True)
class UKMOUKVInspectRequest:
    bbox: BoundingBox
    hours: int
    cycle: str = "auto"
    date: str | None = None
    step_hours: int = 1
    weather_grid_spacing_deg: float = 0.025
    max_keys: int = 200
    refresh_source_index: bool = False


@dataclass(frozen=True)
class UKMOUKVNetCDFInspectRequest:
    bbox: BoundingBox
    hours: int
    download_directory: Path
    cycle: str = "auto"
    date: str | None = None
    step_hours: int = 1
    max_keys: int = 400
    refresh: bool = False
    extract_sample: bool = False
    weather_grid_spacing_deg: float = 0.025
    timeout_seconds: float = 120.0


@dataclass(frozen=True)
class UKMOUKVVerifyRequest:
    bbox: BoundingBox
    grib: Path
    hours: int
    download_directory: Path
    cycle: str = "auto"
    date: str | None = None
    step_hours: int = 1
    weather_grid_spacing_deg: float = 0.025
    tolerance: float = 0.05
    refresh: bool = False
    timeout_seconds: float = 120.0


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
class CopernicusGlobalWaveRequest:
    bbox: BoundingBox
    output: Path
    start: datetime
    hours: int
    step_hours: int = 3
    username: str = ""
    password: str = ""
    download_directory: Path | None = None
    overwrite: bool = False
    dry_run: bool = False
    timeout_seconds: float = 180.0
    grid_spacing_deg: float | None = None


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


@dataclass(frozen=True)
class S3ObjectInfo:
    key: str
    size: int
    last_modified: str | None = None

    @property
    def url(self) -> str:
        return UKMO_UKV_S3_ENDPOINT + quote(self.key)

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "size": self.size, "last_modified": self.last_modified, "url": self.url}


@dataclass(frozen=True)
class S3ListResult:
    prefixes: list[str]
    objects: list[S3ObjectInfo]
    truncated: bool
    next_token: str | None = None


@dataclass(frozen=True)
class UKVRegriddedDataset:
    grid: Any
    forecast_hours: list[int]
    fields: dict[tuple[int, str], Any]
    source_files: dict[str, dict[str, Any]]
    missing_percent: dict[tuple[int, str], float]


@dataclass(frozen=True)
class WaveRegriddedDataset:
    grid: Any
    forecast_hours: list[int]
    fields: dict[tuple[int, str], Any]
    variable_mapping: dict[str, str]
    missing_percent: dict[tuple[int, str], float]
    valid_cell_count: dict[tuple[int, str], int]


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
            id="copernicus_global_waves",
            label="Copernicus Marine Global Waves forecast",
            source="Copernicus Marine",
            format="NetCDF source, converted to OpenCPN GRIB2",
            account="Copernicus Marine account required",
            description=(
                "Global Ocean Waves Analysis and Forecast product "
                f"{COPERNICUS_GLOBAL_WAVE_PRODUCT_ID}; 3-hourly wave height, primary/peak period, and wave direction."
            ),
        ),
        WeatherProvider(
            id="ukmo_ukv",
            label="Met Office UKV 2 km forecast",
            source="Met Office AWS/Open Data",
            format="NetCDF source, converted to OpenCPN GRIB",
            account="free/no account if using AWS/Open Data",
            description=(
                "High-resolution UK/Ireland short-range forecast. Good candidate for Irish Sea coastal routing. "
                "CLI generation regrids the projected UKV NetCDF source to regular latitude/longitude GRIB2."
            ),
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


def generate_copernicus_global_wave_grib(
    request: CopernicusGlobalWaveRequest,
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> WeatherGenerateResult:
    request.bbox.validate()
    _validate_copernicus_global_wave_request(request)
    output = request.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")
    if output.exists() and not request.overwrite:
        raise ValidationError(f"output already exists: {output}; use --overwrite to replace it")

    download_dir = (request.download_directory or output.parent / "copernicus_wave_downloads").expanduser()
    requested_start = request.start.astimezone(timezone.utc)
    requested_end = requested_start + timedelta(hours=request.hours)
    wave_start, wave_end, forecast_hours = _copernicus_wave_time_window(
        requested_start,
        requested_end,
        request.step_hours,
    )
    download_filename = f"copernicus_global_waves_{wave_start:%Y%m%dT%H%MZ}_{int((wave_end - wave_start).total_seconds() // 3600)}h.nc"

    if request.dry_run:
        return WeatherGenerateResult(
            provider="copernicus_global_waves",
            source=COPERNICUS_GLOBAL_WAVE_SOURCE_LABEL,
            model=COPERNICUS_GLOBAL_WAVE_DATASET_ID,
            cycle=WeatherCycle(wave_start.strftime("%Y%m%d"), f"{wave_start.hour:02d}"),
            bbox=request.bbox,
            forecast_hours=forecast_hours,
            output=output,
            byte_count=0,
            message_count=0,
            inspection={
                "stream_valid": False,
                "message_count": 0,
                "dry_run": True,
                "requested_start": requested_start.isoformat(),
                "requested_end": requested_end.isoformat(),
                "actual_wave_valid_times": _wave_valid_time_strings(wave_start, forecast_hours),
            },
            urls=[],
            variables_levels={
                "product": COPERNICUS_GLOBAL_WAVE_PRODUCT_ID,
                "dataset_id": COPERNICUS_GLOBAL_WAVE_DATASET_ID,
                "variables": list(COPERNICUS_GLOBAL_WAVE_VARIABLES),
            },
        )

    if wave_start != requested_start:
        _progress(
            progress_callback,
            "adjusted Copernicus Global Waves time range",
            {
                "requested_start": requested_start.isoformat(),
                "requested_end": requested_end.isoformat(),
                "wave_start": wave_start.isoformat(),
                "wave_end": wave_end.isoformat(),
                "step_hours": request.step_hours,
                "valid_times": _wave_valid_time_strings(wave_start, forecast_hours),
            },
        )

    _progress(
        progress_callback,
        "downloading Copernicus Global Waves NetCDF",
        {
            "dataset_id": COPERNICUS_GLOBAL_WAVE_DATASET_ID,
            "variables": list(COPERNICUS_GLOBAL_WAVE_VARIABLES),
            "bbox": request.bbox.__dict__,
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "start": wave_start.isoformat(),
            "end": wave_end.isoformat(),
        },
    )
    download = download_copernicus_subset(
        CopernicusDownloadRequest(
            bbox=request.bbox,
            start=wave_start,
            end=wave_end,
            output_directory=download_dir,
            output_filename=download_filename,
            username=request.username,
            password=request.password,
            dataset_id=COPERNICUS_GLOBAL_WAVE_DATASET_ID,
            variables=COPERNICUS_GLOBAL_WAVE_VARIABLES,
            overwrite=True,
            dry_run=False,
        ),
        progress_callback=progress_callback,
    )
    _progress(progress_callback, "downloaded Copernicus Global Waves NetCDF", {"path": str(download.path)})

    dataset = _load_copernicus_wave_dataset(
        download.path,
        request.bbox,
        wave_start,
        forecast_hours,
        request.grid_spacing_deg,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        _write_wave_grib2(dataset, wave_start, tmp_path, progress_callback=progress_callback)
        scan = scan_grib_messages(tmp_path)
        expected_messages = len(forecast_hours) * 3
        if scan.message_count != expected_messages:
            raise ValidationError(f"Copernicus wave GRIB message count mismatch: expected {expected_messages}, got {scan.message_count}")
        inspection = inspect_grib(tmp_path)
        if not inspection.get("stream_valid"):
            raise ValidationError("Copernicus wave GRIB stream validation failed")
        inspection["missing_percent"] = {
            f"f{hour:03d}_{short_name}": percent
            for (hour, short_name), percent in dataset.missing_percent.items()
        }
        inspection["valid_cell_count"] = {
            f"f{hour:03d}_{short_name}": count
            for (hour, short_name), count in dataset.valid_cell_count.items()
        }
        inspection["requested_start"] = requested_start.isoformat()
        inspection["requested_end"] = requested_end.isoformat()
        inspection["actual_wave_valid_times"] = _wave_valid_time_strings(wave_start, forecast_hours)
        tmp_path.replace(output)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()

    return WeatherGenerateResult(
        provider="copernicus_global_waves",
        source=COPERNICUS_GLOBAL_WAVE_SOURCE_LABEL,
        model=COPERNICUS_GLOBAL_WAVE_DATASET_ID,
        cycle=WeatherCycle(wave_start.strftime("%Y%m%d"), f"{wave_start.hour:02d}"),
        bbox=request.bbox,
        forecast_hours=forecast_hours,
        output=output,
        byte_count=scan.byte_count,
        message_count=scan.message_count,
        inspection=inspection,
        urls=[str(download.path)],
        variables_levels={
            "product": COPERNICUS_GLOBAL_WAVE_PRODUCT_ID,
            "dataset_id": COPERNICUS_GLOBAL_WAVE_DATASET_ID,
            "variables": list(COPERNICUS_GLOBAL_WAVE_VARIABLES),
            "output_short_names": ["swh", "perpw", "dirpw"],
            "missing_values": "GRIB2 bitmap/missingValue for masked or NaN land cells",
            "requested_start": requested_start.isoformat(),
            "requested_end": requested_end.isoformat(),
            "actual_wave_valid_times": _wave_valid_time_strings(wave_start, forecast_hours),
        },
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


def generate_ukmo_ukv_weather_grib(
    request: UKMOUKVWeatherRequest,
    *,
    http_get: HttpGet | None = None,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> WeatherGenerateResult:
    request.bbox.validate()
    _validate_ukmo_ukv_request(request)
    http_get = http_get or _http_get
    output = request.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")
    if output.exists() and not request.overwrite:
        raise ValidationError(f"output already exists: {output}; use --overwrite to replace it")
    forecast_hours = ukmo_ukv_forecast_hour_sequence(request.hours, request.step_hours)
    _progress(
        progress_callback,
        "selecting Met Office UKV source files",
        {
            "bbox": request.bbox.__dict__,
            "hours": request.hours,
            "step_hours": request.step_hours,
            "actual_forecast_hours": forecast_hours,
            "weather_grid_spacing_deg": request.weather_grid_spacing_deg,
        },
    )
    if request.step_hours == 1 and request.hours > 54:
        _progress(
            progress_callback,
            "UKV weather fields are hourly to 54h and 3-hourly thereafter.",
            {"requested_hours": request.hours, "requested_step_hours": request.step_hours, "forecast_hours": forecast_hours},
        )

    if request.dry_run:
        cycle_name = _ukv_cycle_candidates_for_request(
            UKMOUKVNetCDFInspectRequest(
                bbox=request.bbox,
                hours=request.hours,
                download_directory=Path("."),
                step_hours=request.step_hours,
                cycle=request.cycle,
                date=request.date,
                weather_grid_spacing_deg=request.weather_grid_spacing_deg,
                timeout_seconds=request.timeout_seconds,
            )
        )[0]
        planned = _weather_cycle_from_ukv_cycle_name(cycle_name)
        return WeatherGenerateResult(
            provider="ukmo_ukv",
            source=UKMO_UKV_SOURCE_LABEL,
            model="uk_deterministic_2km",
            cycle=planned,
            bbox=request.bbox,
            forecast_hours=forecast_hours,
            output=output,
            byte_count=0,
            message_count=0,
            inspection={"stream_valid": False, "message_count": 0, "dry_run": True},
            urls=[],
            variables_levels={"preset": request.preset, "weather_grid_spacing_deg": request.weather_grid_spacing_deg},
        )

    with tempfile.TemporaryDirectory(prefix="ukv-source.") as source_tmp:
        selected_cycle, downloaded, file_inspections, cycle_errors = _download_and_inspect_ukv_source_files(
            bbox=request.bbox,
            hours=request.hours,
            step_hours=request.step_hours,
            cycle=request.cycle,
            date=request.date,
            download_directory=Path(source_tmp),
            weather_grid_spacing_deg=request.weather_grid_spacing_deg,
            refresh=True,
            extract_sample=False,
            timeout_seconds=request.timeout_seconds,
            http_get=http_get,
        )
        _progress(progress_callback, "selected Met Office UKV cycle", {"cycle": selected_cycle})
        if cycle_errors:
            _progress(progress_callback, "UKV cycle fallback details", {"errors": cycle_errors})
        dataset = _regrid_ukv_source_fields(
            downloaded,
            file_inspections,
            request.bbox,
            request.weather_grid_spacing_deg,
            forecast_hours,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
                tmp_path = Path(tmp.name)
            _write_ukv_grib2(dataset, selected_cycle, tmp_path, progress_callback=progress_callback)
            scan = scan_grib_messages(tmp_path)
            expected_messages = len(forecast_hours) * 4
            if scan.message_count != expected_messages:
                raise ValidationError(f"UKV GRIB message count mismatch: expected {expected_messages}, got {scan.message_count}")
            inspection = inspect_grib(tmp_path)
            if not inspection.get("stream_valid"):
                raise ValidationError("UKV GRIB stream validation failed")
            tmp_path.replace(output)
            tmp_path = None
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()

    cycle_obj = _weather_cycle_from_ukv_cycle_name(selected_cycle)
    return WeatherGenerateResult(
        provider="ukmo_ukv",
        source=UKMO_UKV_SOURCE_LABEL,
        model="uk_deterministic_2km",
        cycle=cycle_obj,
        bbox=request.bbox,
        forecast_hours=forecast_hours,
        output=output,
        byte_count=scan.byte_count,
        message_count=scan.message_count,
        inspection=inspection,
        urls=[info["url"] for info in downloaded.values()],
        variables_levels={
            "source_fields": sorted({str(info["field"]) for info in downloaded.values()}),
            "output_short_names": ["10u", "10v", "prmsl", "2t"],
            "weather_grid_spacing_deg": request.weather_grid_spacing_deg,
        },
    )


def inspect_ukmo_ukv_source(request: UKMOUKVInspectRequest) -> dict[str, Any]:
    request.bbox.validate()
    _validate_ukmo_ukv_request(
        UKMOUKVWeatherRequest(
            bbox=request.bbox,
            output=Path("ukmo_ukv_inspect.grb"),
            hours=request.hours,
            step_hours=request.step_hours,
            cycle=request.cycle,
            date=request.date,
            weather_grid_spacing_deg=request.weather_grid_spacing_deg,
        )
    )
    candidate_hours = ukmo_ukv_forecast_hour_sequence(request.hours, request.step_hours)
    discovery = discover_ukmo_ukv_source(max_keys=request.max_keys)
    candidate_files = discovery["candidate_files"]
    cycle_candidates = _extract_ukv_cycles(candidate_files)
    variable_candidates = _candidate_variables_from_keys(candidate_files)
    return {
        "provider": "ukmo_ukv",
        "source": UKMO_UKV_SOURCE_LABEL,
        "status": "blocked",
        "implemented": False,
        "selected_cycle": _select_discovered_ukv_cycle(cycle_candidates, request),
        "source_bucket": f"s3://{UKMO_UKV_BUCKET}/",
        "source_region": UKMO_UKV_REGION,
        "source_paths_or_urls": [item["url"] for item in candidate_files[:20]],
        "top_level_prefixes": discovery["top_level_prefixes"],
        "likely_ukv_prefixes": discovery["likely_ukv_prefixes"],
        "available_model_runs": cycle_candidates,
        "available_forecast_hours": _extract_forecast_hours(candidate_files),
        "requested_forecast_hours": candidate_hours,
        "candidate_files": candidate_files[:50],
        "available_near_surface_variables": sorted({v for values in variable_candidates.values() for v in values}),
        "coordinate_variables": "(requires sample NetCDF download)",
        "grid_mapping": "(requires sample NetCDF download)",
        "source_grid_shape": None,
        "source_lat_lon_coverage": None,
        "bbox_intersects_domain": True,
        "candidate_variables": variable_candidates,
        "domain": UKMO_UKV_DOMAIN.__dict__,
        "weather_grid_spacing_deg": request.weather_grid_spacing_deg,
        "anonymous_listing": discovery["anonymous_listing"],
        "listing_error": discovery["error"],
        "blocker": (
            "UKV source discovery can list anonymous S3 objects, but GRIB generation remains disabled until "
            "sample NetCDF coordinate/projection metadata, variable mappings, and numeric source-to-GRIB "
            "roundtrip verification are implemented."
        ),
    }


def inspect_ukmo_ukv_netcdf(
    request: UKMOUKVNetCDFInspectRequest,
    *,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    """Download and inspect a minimal set of UKV NetCDF source files.

    This intentionally stops at source metadata and sample statistics. It does
    not enable UKV GRIB output.
    """

    request.bbox.validate()
    _validate_ukmo_ukv_request(
        UKMOUKVWeatherRequest(
            bbox=request.bbox,
            output=Path("ukmo_ukv_inspect.grb"),
            hours=request.hours,
            step_hours=request.step_hours,
            cycle=request.cycle,
            date=request.date,
        )
    )
    http_get = http_get or _http_get
    requested_hours = ukmo_ukv_forecast_hour_sequence(request.hours, request.step_hours)
    selected_cycle, downloaded, file_inspections, cycle_errors = _download_and_inspect_ukv_source_files(
        bbox=request.bbox,
        hours=request.hours,
        step_hours=request.step_hours,
        cycle=request.cycle,
        date=request.date,
        download_directory=request.download_directory,
        weather_grid_spacing_deg=request.weather_grid_spacing_deg,
        refresh=request.refresh,
        extract_sample=request.extract_sample,
        timeout_seconds=request.timeout_seconds,
        http_get=http_get,
    )

    coordinate_summary = _summarize_ukv_coordinates(file_inspections, request.bbox)
    time_summary = _summarize_ukv_times(file_inspections, requested_hours)
    variable_mappings = _ukv_variable_mapping_candidates(file_inspections)
    wind_direction = _infer_ukv_wind_direction_convention(file_inspections)
    wind_uv_stats = _ukv_wind_uv_sample_stats(downloaded, file_inspections) if request.extract_sample else None
    regrid_sample = (
        _ukv_regrid_sample(downloaded, file_inspections, request.bbox, request.weather_grid_spacing_deg)
        if request.extract_sample
        else None
    )
    return {
        "provider": "ukmo_ukv",
        "source": UKMO_UKV_SOURCE_LABEL,
        "status": "metadata-only",
        "implemented": False,
        "selected_cycle": selected_cycle,
        "cycle_selection_errors": cycle_errors,
        "source_bucket": f"s3://{UKMO_UKV_BUCKET}/",
        "source_region": UKMO_UKV_REGION,
        "download_directory": str(request.download_directory.expanduser()),
        "requested_forecast_hours": requested_hours,
        "downloaded_files": downloaded,
        "files": file_inspections,
        "coordinate_summary": coordinate_summary,
        "time_summary": time_summary,
        "variable_mappings": variable_mappings,
        "wind_direction_convention": wind_direction,
        "wind_uv_sample_stats": wind_uv_stats,
        "regrid_sample": regrid_sample,
        "crop_feasibility": coordinate_summary.get("crop_feasibility", {}),
        "generation_enabled": False,
        "blocker": (
            "UKV NetCDF source metadata can be inspected, but GRIB generation remains disabled until "
            "projection/regridding and numeric source-to-GRIB verification are implemented."
        ),
    }


def verify_ukmo_ukv_grib(
    request: UKMOUKVVerifyRequest,
    *,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    request.bbox.validate()
    if request.tolerance <= 0:
        raise ValidationError("--tolerance must be greater than zero")
    http_get = http_get or _http_get
    grib_path = request.grib.expanduser()
    if not grib_path.exists():
        raise ValidationError(f"UKV GRIB does not exist: {grib_path}")
    scan = scan_grib_messages(grib_path)
    if scan.message_count <= 0:
        raise ValidationError("UKV GRIB contains no messages")
    grib_fields = _read_ukv_grib_fields(grib_path)
    if request.cycle == "auto":
        cycle_dt = grib_fields["reference_time"]
        cycle = f"{cycle_dt.strftime('%Y%m%dT%H%MZ')}"
        date = cycle_dt.strftime("%Y%m%d")
        cycle_hour = f"{cycle_dt.hour:02d}"
    else:
        if request.date is None:
            raise ValidationError("--date YYYYMMDD is required when --cycle is explicit")
        date = request.date
        cycle_hour = request.cycle
        cycle = f"{date}T{cycle_hour}00Z"

    forecast_hours = ukmo_ukv_forecast_hour_sequence(request.hours, request.step_hours)
    selected_cycle, downloaded, file_inspections, cycle_errors = _download_and_inspect_ukv_source_files(
        bbox=request.bbox,
        hours=request.hours,
        step_hours=request.step_hours,
        cycle=cycle_hour,
        date=date,
        download_directory=request.download_directory,
        weather_grid_spacing_deg=request.weather_grid_spacing_deg,
        refresh=request.refresh,
        extract_sample=False,
        timeout_seconds=request.timeout_seconds,
        http_get=http_get,
    )
    if selected_cycle != cycle:
        raise ValidationError(f"UKV verification selected source cycle {selected_cycle}, but GRIB reference cycle is {cycle}")
    expected = _regrid_ukv_source_fields(
        downloaded,
        file_inspections,
        request.bbox,
        request.weather_grid_spacing_deg,
        forecast_hours,
    )
    grid_checks = _verify_ukv_grib_grid(grib_fields["grid"], expected.grid, request.bbox)
    comparisons: dict[str, Any] = {}
    failures: list[str] = []
    for hour in forecast_hours:
        for short_name in ("10u", "10v", "prmsl", "2t"):
            key = (hour, short_name)
            if key not in grib_fields["fields"]:
                failures.append(f"missing GRIB field {short_name} f{hour:03d}")
                continue
            comparison = _compare_arrays(expected.fields[key], grib_fields["fields"][key])
            comparisons[f"{short_name}_f{hour:03d}"] = comparison
            if comparison["max_abs_error"] > request.tolerance:
                failures.append(
                    f"{short_name} f{hour:03d} max_abs_error {comparison['max_abs_error']:.6g} exceeds tolerance {request.tolerance:g}"
                )
    result = {
        "provider": "ukmo_ukv",
        "source": UKMO_UKV_SOURCE_LABEL,
        "grib": str(grib_path),
        "selected_cycle": selected_cycle,
        "cycle_selection_errors": cycle_errors,
        "forecast_hours": forecast_hours,
        "message_count": scan.message_count,
        "expected_message_count": len(forecast_hours) * 4,
        "grid_checks": grid_checks,
        "comparisons": comparisons,
        "tolerance": request.tolerance,
        "passed": not failures and grid_checks["passed"] and scan.message_count >= len(forecast_hours) * 4,
        "failures": failures + grid_checks["failures"],
    }
    if not result["passed"]:
        raise ValidationError("UKV GRIB verification failed: " + "; ".join(result["failures"]))
    return result


def discover_ukmo_ukv_source(
    *,
    max_keys: int = 200,
    http_get: HttpGet | None = None,
    timeout_seconds: float = 30.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    http_get = http_get or _http_get
    max_keys = max(1, min(max_keys, 1000))
    top_level_prefixes: list[str] = []
    visited_prefixes: list[str] = []
    likely_prefixes: list[str] = []
    objects: list[S3ObjectInfo] = []
    error: str | None = None
    anonymous_listing = False
    try:
        root = _list_s3_prefix("", delimiter="/", max_keys=max_keys, http_get=http_get, timeout_seconds=timeout_seconds)
        anonymous_listing = True
        top_level_prefixes = root.prefixes
        has_likely_top_level = any(_looks_like_ukv_prefix(prefix) for prefix in root.prefixes)
        recent_prefixes = _recent_ukv_run_prefixes(now or datetime.now(timezone.utc))
        queue = recent_prefixes + sorted(root.prefixes, key=lambda prefix: (not _looks_like_ukv_prefix(prefix), prefix))
        objects.extend(root.objects)
        while queue and len(visited_prefixes) < max_keys:
            prefix = queue.pop(0)
            if prefix in visited_prefixes:
                continue
            if has_likely_top_level and not _looks_like_ukv_prefix(prefix):
                continue
            if len(visited_prefixes) >= max_keys:
                break
            visited_prefixes.append(prefix)
            try:
                listing = _list_s3_prefix(prefix, delimiter="/", max_keys=max_keys, http_get=http_get, timeout_seconds=timeout_seconds)
            except ValidationError:
                continue
            if _looks_like_ukv_prefix(prefix) and (listing.objects or listing.prefixes or prefix in top_level_prefixes):
                likely_prefixes.append(prefix)
            objects.extend(listing.objects)
            for child in listing.prefixes:
                if len(visited_prefixes) + len(queue) >= max_keys * 2:
                    break
                if child not in visited_prefixes and child not in queue:
                    queue.append(child)
                if _looks_like_ukv_prefix(child):
                    likely_prefixes.append(child)
            if len(objects) >= max_keys:
                break
    except ValidationError as exc:
        error = str(exc)
    candidate_objects = [obj for obj in objects if _looks_like_ukv_file(obj.key)]
    if not candidate_objects:
        likely_object_prefixes = tuple(sorted(set(likely_prefixes)))
        candidate_objects = [
            obj
            for obj in objects
            if obj.key.lower().endswith((".nc", ".nc4", ".netcdf"))
            and (not likely_object_prefixes or obj.key.startswith(likely_object_prefixes))
        ]
    return {
        "bucket": UKMO_UKV_BUCKET,
        "region": UKMO_UKV_REGION,
        "anonymous_listing": anonymous_listing,
        "top_level_prefixes": top_level_prefixes,
        "visited_prefixes": visited_prefixes,
        "likely_ukv_prefixes": sorted(set(likely_prefixes)),
        "candidate_files": [obj.as_dict() for obj in candidate_objects[:max_keys]],
        "object_count_seen": len(objects),
        "error": error,
    }


def _list_s3_prefix(
    prefix: str,
    *,
    delimiter: str | None,
    max_keys: int,
    http_get: HttpGet,
    timeout_seconds: float,
) -> S3ListResult:
    query: dict[str, str] = {"list-type": "2", "max-keys": str(max(1, min(max_keys, 1000)))}
    if prefix:
        query["prefix"] = prefix
    if delimiter is not None:
        query["delimiter"] = delimiter
    url = UKMO_UKV_S3_ENDPOINT + "?" + urlencode(query)
    try:
        data = http_get(url, timeout_seconds)
    except HTTPError as exc:
        raise ValidationError(f"UKV S3 listing failed for prefix {prefix!r}: HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValidationError(f"UKV S3 listing failed for prefix {prefix!r}: {exc.reason}") from exc
    except OSError as exc:
        raise ValidationError(f"UKV S3 listing failed for prefix {prefix!r}: {exc}") from exc
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        sample = data[:200].decode("utf-8", errors="replace")
        raise ValidationError(f"UKV S3 listing returned non-XML data for prefix {prefix!r}: {sample!r}") from exc

    prefixes: list[str] = []
    objects: list[S3ObjectInfo] = []
    truncated = False
    next_token: str | None = None
    for child in root:
        tag = _xml_local_name(child.tag)
        if tag == "CommonPrefixes":
            value = _xml_child_text(child, "Prefix")
            if value:
                prefixes.append(value)
        elif tag == "Contents":
            key = _xml_child_text(child, "Key")
            if not key:
                continue
            size_text = _xml_child_text(child, "Size") or "0"
            try:
                size = int(size_text)
            except ValueError:
                size = 0
            objects.append(S3ObjectInfo(key=key, size=size, last_modified=_xml_child_text(child, "LastModified")))
        elif tag == "IsTruncated":
            truncated = (child.text or "").strip().lower() == "true"
        elif tag == "NextContinuationToken":
            next_token = (child.text or "").strip() or None
    return S3ListResult(prefixes=prefixes, objects=objects, truncated=truncated, next_token=next_token)


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_child_text(element: ET.Element, child_name: str) -> str | None:
    for child in element:
        if _xml_local_name(child.tag) == child_name:
            return (child.text or "").strip()
    return None


def _looks_like_ukv_prefix(prefix: str) -> bool:
    lower = prefix.lower()
    return any(token in lower for token in ("ukv", "uk-deterministic", "uk_deterministic", "uk-deterministic-2km", "uk/"))


def _looks_like_ukv_file(key: str) -> bool:
    lower = key.lower()
    if not lower.endswith((".nc", ".nc4", ".netcdf")):
        return False
    return any(token in lower for token in ("ukv", "uk-deterministic", "uk_deterministic", "deterministic"))


def _recent_ukv_run_prefixes(now: datetime) -> list[str]:
    dt = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    cycles = ("00", "03", "06", "09", "12", "15", "18", "21")
    prefixes: list[str] = []
    for day_offset in range(0, 5):
        day = (dt - timedelta(days=day_offset)).strftime("%Y%m%d")
        for cycle in reversed(cycles):
            prefixes.append(f"uk-deterministic-2km/{day}T{cycle}00Z/")
    return prefixes


def _extract_ukv_cycles(candidate_files: list[dict[str, Any]]) -> list[str]:
    cycles: set[str] = set()
    for item in candidate_files:
        key = str(item.get("key", ""))
        match = re.search(r"(?:^|/)uk-deterministic-2km/(20\d{6})T([012]\d)00Z/", key)
        if match and match.group(2) in {"00", "03", "06", "09", "12", "15", "18", "21"}:
            cycles.add(f"{match.group(1)}T{match.group(2)}00Z")
    return sorted(cycles, reverse=True)


def _extract_forecast_hours(candidate_files: list[dict[str, Any]]) -> list[int]:
    hours: set[int] = set()
    patterns = (
        re.compile(r"(?:^|[_.\-/])f(\d{1,3})(?:[_.\-/]|$)", re.IGNORECASE),
        re.compile(r"(?:^|[_.\-/])t\+?(\d{1,3})(?:[_.\-/]|$)", re.IGNORECASE),
        re.compile(r"pt0*(\d{1,3})h", re.IGNORECASE),
        re.compile(r"(?:forecast|fcst|step)[_.\-]?(\d{1,3})", re.IGNORECASE),
    )
    for item in candidate_files:
        key = str(item.get("key", ""))
        for pattern in patterns:
            for match in pattern.finditer(key):
                value = int(match.group(1))
                if 0 <= value <= 240:
                    hours.add(value)
    return sorted(hours)


def _candidate_variables_from_keys(candidate_files: list[dict[str, Any]]) -> dict[str, list[str]]:
    fields = {
        "wind_u": ("10u", "u10", "uwind", "u_wind", "x_wind", "eastward_wind", "u-component"),
        "wind_v": ("10v", "v10", "vwind", "v_wind", "y_wind", "northward_wind", "v-component"),
        "wind_speed": ("wind_speed_at_10m", "wind_speed"),
        "wind_direction": ("wind_direction_at_10m", "wind_direction"),
        "pressure_msl": ("mslp", "prmsl", "msl", "mean_sea_level_pressure", "pressure"),
        "temperature_near_surface": ("t2m", "2t", "tas", "air_temperature", "temperature", "temp"),
        "gust": ("gust", "wind_gust"),
        "cloud_cover": ("tcc", "cloud", "cloud_area_fraction"),
        "precipitation": ("precip", "rain", "apcp", "tp", "precipitation"),
    }
    result: dict[str, list[str]] = {name: [] for name in fields}
    for item in candidate_files:
        key = str(item.get("key", ""))
        lower = key.lower()
        for name, tokens in fields.items():
            if any(token in lower for token in tokens) and key not in result[name]:
                result[name].append(key)
    return {name: values[:10] for name, values in result.items()}


def _select_discovered_ukv_cycle(cycles: list[str], request: UKMOUKVInspectRequest) -> str:
    if request.cycle != "auto":
        if not request.date:
            return "(explicit cycle requested but --date missing)"
        return f"{request.date}T{request.cycle}00Z"
    return cycles[0] if cycles else "(not discovered)"


def _select_ukv_required_objects(candidate_files: list[dict[str, Any]], selected_cycle: str) -> dict[str, dict[str, Any] | None]:
    prefix = f"uk-deterministic-2km/{selected_cycle}/"
    selected: dict[str, dict[str, Any] | None] = {}
    for field_name, spec in UKMO_UKV_REQUIRED_SOURCE_FIELDS.items():
        token = str(spec["filename_token"])
        matches = [
            item
            for item in candidate_files
            if str(item.get("key", "")).startswith(prefix)
            and token in str(item.get("key", ""))
            and "PT0000H00M" in str(item.get("key", ""))
        ]
        if not matches:
            matches = [
                item
                for item in candidate_files
                if str(item.get("key", "")).startswith(prefix)
                and token in str(item.get("key", ""))
            ]
        selected[field_name] = matches[0] if matches else None
    return selected


def _download_and_inspect_ukv_source_files(
    *,
    bbox: BoundingBox,
    hours: int,
    step_hours: int,
    cycle: str,
    date: str | None,
    download_directory: Path,
    weather_grid_spacing_deg: float,
    refresh: bool,
    extract_sample: bool,
    timeout_seconds: float,
    http_get: HttpGet,
) -> tuple[str, dict[str, dict[str, Any]], dict[str, Any], list[str]]:
    request = UKMOUKVNetCDFInspectRequest(
        bbox=bbox,
        hours=hours,
        step_hours=step_hours,
        cycle=cycle,
        date=date,
        download_directory=download_directory,
        weather_grid_spacing_deg=weather_grid_spacing_deg,
        refresh=refresh,
        extract_sample=extract_sample,
        timeout_seconds=timeout_seconds,
    )
    requested_hours = ukmo_ukv_forecast_hour_sequence(hours, step_hours)
    selected_cycle, selected_objects, cycle_errors = _select_complete_ukv_cycle(
        request,
        requested_hours,
        http_get=http_get,
    )
    download_directory = download_directory.expanduser()
    download_directory.mkdir(parents=True, exist_ok=True)
    downloaded: dict[str, dict[str, Any]] = {}
    for item_name, item in selected_objects.items():
        path = download_directory / Path(str(item["key"])).name
        expected_size = int(item.get("size") or 0)
        reused = path.exists() and not refresh and (expected_size <= 0 or path.stat().st_size == expected_size)
        if not reused:
            data = http_get(str(item["url"]), timeout_seconds)
            if not data:
                raise ValidationError(f"UKV download returned empty response for {item['key']}")
            tmp_path = path.with_name(path.name + ".tmp")
            tmp_path.write_bytes(data)
            if expected_size > 0 and tmp_path.stat().st_size != expected_size:
                tmp_path.unlink(missing_ok=True)
                raise ValidationError(
                    f"UKV download size mismatch for {item['key']}: expected {expected_size}, got {len(data)}"
                )
            tmp_path.replace(path)
        downloaded[item_name] = {
            "field": item["field"],
            "forecast_hour": item["forecast_hour"],
            "source_key": item["key"],
            "url": item["url"],
            "path": str(path),
            "expected_size": expected_size,
            "size": path.stat().st_size,
            "reused": reused,
        }

    file_inspections: dict[str, Any] = {}
    for item_name, info in downloaded.items():
        file_inspections[item_name] = _inspect_ukv_netcdf_file(
            Path(info["path"]),
            bbox,
            extract_sample=extract_sample,
        )
    return selected_cycle, downloaded, file_inspections, cycle_errors


def _select_complete_ukv_cycle(
    request: UKMOUKVNetCDFInspectRequest,
    forecast_hours: list[int],
    *,
    http_get: HttpGet,
) -> tuple[str, dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    for cycle in _ukv_cycle_candidates_for_request(request):
        selected: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for hour in forecast_hours:
            for field_name, spec in UKMO_UKV_REQUIRED_SOURCE_FIELDS.items():
                key = _ukv_source_key(cycle, hour, str(spec["filename_token"]))
                item = _s3_object_for_key(key, http_get=http_get, timeout_seconds=request.timeout_seconds)
                if item is None:
                    missing.append(f"{field_name} f{hour:03d}")
                    continue
                selected[f"{field_name}_h{hour:03d}"] = {
                    **item.as_dict(),
                    "field": field_name,
                    "forecast_hour": hour,
                }
        if not missing:
            return cycle, selected, errors
        errors.append(f"{cycle}: missing {', '.join(missing[:8])}" + (" ..." if len(missing) > 8 else ""))
        if request.cycle != "auto":
            break
    raise ValidationError(
        "could not find a complete UKV cycle for required fields/hours. " + "; ".join(errors)
    )


def _ukv_cycle_candidates_for_request(request: UKMOUKVNetCDFInspectRequest) -> list[str]:
    if request.cycle != "auto":
        if not request.date:
            raise ValidationError("--date YYYYMMDD is required when --cycle is explicit")
        return [f"{request.date}T{request.cycle}00Z"]
    now = datetime.now(timezone.utc)
    return [prefix.removeprefix("uk-deterministic-2km/").removesuffix("/") for prefix in _recent_ukv_run_prefixes(now)]


def _ukv_source_key(cycle: str, forecast_hour: int, filename_token: str) -> str:
    cycle_dt = datetime.strptime(cycle, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)
    valid_dt = cycle_dt + timedelta(hours=forecast_hour)
    return (
        f"uk-deterministic-2km/{cycle}/"
        f"{valid_dt.strftime('%Y%m%dT%H%MZ')}-PT{forecast_hour:04d}H00M-{filename_token}.nc"
    )


def _s3_object_for_key(
    key: str,
    *,
    http_get: HttpGet,
    timeout_seconds: float,
) -> S3ObjectInfo | None:
    try:
        listing = _list_s3_prefix(key, delimiter=None, max_keys=1, http_get=http_get, timeout_seconds=timeout_seconds)
    except ValidationError:
        return None
    for obj in listing.objects:
        if obj.key == key:
            return obj
    return None


def _inspect_ukv_netcdf_file(path: Path, bbox: BoundingBox, *, extract_sample: bool) -> dict[str, Any]:
    try:
        import numpy as np
        import xarray as xr
    except ImportError as exc:
        raise MissingDependencyError(
            "UKV NetCDF inspection requires xarray and numpy; install the netcdf/weather extras."
        ) from exc

    with xr.open_dataset(path) as ds:
        dimensions = {name: int(size) for name, size in ds.sizes.items()}
        coordinates = list(ds.coords)
        data_variables = list(ds.data_vars)
        variables: dict[str, Any] = {}
        for name in list(ds.variables):
            var = ds[name]
            variables[name] = {
                "dims": list(var.dims),
                "shape": [int(v) for v in var.shape],
                "units": _attr_text(var.attrs, "units"),
                "standard_name": _attr_text(var.attrs, "standard_name"),
                "long_name": _attr_text(var.attrs, "long_name"),
                "grid_mapping": _attr_text(var.attrs, "grid_mapping"),
                "attrs": {str(key): _json_safe_attr_value(value) for key, value in var.attrs.items()},
            }
        data_var_names = [name for name in data_variables if ds[name].ndim >= 1]
        primary_name = _choose_primary_data_variable(ds, path)
        primary = ds[primary_name] if primary_name else None
        grid_mapping_name = _attr_text(primary.attrs, "grid_mapping") if primary is not None else None
        grid_mapping = variables.get(grid_mapping_name or "", None)
        latlon = _lat_lon_bounds(ds)
        xy_summary = _xy_coordinate_summary(ds)
        if not latlon.get("bounds"):
            projected_bounds = _projected_xy_latlon_bounds(xy_summary, grid_mapping)
            if projected_bounds:
                latlon["bounds"] = projected_bounds
                latlon["derived_from_projection"] = True
        time_summary = _time_coordinate_summary(ds)
        crop_index_bounds = _projected_bbox_index_bounds(bbox, xy_summary, grid_mapping)
        sample_stats: dict[str, Any] | None = None
        if extract_sample and primary is not None:
            sample_stats = _sample_data_stats(primary, crop_index_bounds=crop_index_bounds)
        elif primary is not None:
            sample_stats = _sample_data_stats(primary, quick=True)
        bbox_intersection = _bbox_intersects_latlon_bounds(bbox, latlon.get("bounds")) if latlon.get("bounds") else None
        return {
            "path": str(path),
            "file_size": path.stat().st_size,
            "dimensions": dimensions,
            "coordinate_variables": coordinates,
            "data_variables": data_variables,
            "primary_data_variable": primary_name,
            "variables": variables,
            "grid_mapping_name": grid_mapping_name,
            "grid_mapping": grid_mapping,
            "time": time_summary,
            "lat_lon": latlon,
            "xy": xy_summary,
            "grid_type": _classify_ukv_grid(latlon, xy_summary, grid_mapping),
            "bbox_intersects_source_bounds": bbox_intersection,
            "bbox_index_bounds": crop_index_bounds,
            "sample_stats": sample_stats,
        }


def _attr_text(attrs: dict[str, Any], name: str) -> str | None:
    value = attrs.get(name)
    return None if value is None else str(value)


def _json_safe_attr_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_attr_value(item) for item in value]
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


def _choose_primary_data_variable(ds: Any, path: Path) -> str | None:
    candidates = [name for name in ds.data_vars if not str(name).lower().endswith("bounds")]
    if not candidates:
        return None
    stem = path.stem.lower()
    for name in candidates:
        normalized = str(name).lower()
        if normalized in stem or stem.endswith(normalized):
            return str(name)
    candidates.sort(key=lambda name: ds[name].ndim, reverse=True)
    return str(candidates[0])


def _lat_lon_bounds(ds: Any) -> dict[str, Any]:
    import numpy as np

    lat_names = [name for name in ds.variables if str(name).lower() in {"lat", "latitude", "grid_latitude"}]
    lon_names = [name for name in ds.variables if str(name).lower() in {"lon", "longitude", "grid_longitude"}]
    result: dict[str, Any] = {"latitude_variables": lat_names, "longitude_variables": lon_names}
    lat_name = next((name for name in lat_names if "lat" in str(name).lower()), None)
    lon_name = next((name for name in lon_names if "lon" in str(name).lower()), None)
    if lat_name is None or lon_name is None:
        return result
    lat = np.asarray(ds[lat_name].values)
    lon = np.asarray(ds[lon_name].values)
    result.update(
        {
            "latitude_name": str(lat_name),
            "longitude_name": str(lon_name),
            "latitude_shape": list(lat.shape),
            "longitude_shape": list(lon.shape),
            "latitude_range": _finite_range(lat),
            "longitude_range": _finite_range(lon),
            "latitude_monotonic": _is_monotonic(lat),
            "longitude_monotonic": _is_monotonic(lon),
        }
    )
    if result["latitude_range"] and result["longitude_range"]:
        result["bounds"] = {
            "west": result["longitude_range"][0],
            "south": result["latitude_range"][0],
            "east": result["longitude_range"][1],
            "north": result["latitude_range"][1],
        }
    return result


def _xy_coordinate_summary(ds: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for axis, names in {"x": ("x", "projection_x_coordinate"), "y": ("y", "projection_y_coordinate")}.items():
        found = next((name for name in ds.variables if str(name).lower() in names), None)
        if found is None:
            continue
        values = ds[found].values
        result[axis] = {
            "name": str(found),
            "dims": list(ds[found].dims),
            "shape": [int(v) for v in ds[found].shape],
            "units": _attr_text(ds[found].attrs, "units"),
            "standard_name": _attr_text(ds[found].attrs, "standard_name"),
            "range": _finite_range(values),
            "monotonic": _is_monotonic(values),
        }
    return result


def _time_coordinate_summary(ds: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ds.variables:
        lower = str(name).lower()
        if lower not in {"time", "forecast_period", "forecast_reference_time", "forecast_reference_time_bnds"} and "time" not in lower:
            continue
        var = ds[name]
        values = var.values
        result[str(name)] = {
            "dims": list(var.dims),
            "shape": [int(v) for v in var.shape],
            "units": _attr_text(var.attrs, "units"),
            "standard_name": _attr_text(var.attrs, "standard_name"),
            "long_name": _attr_text(var.attrs, "long_name"),
            "values": _compact_values(values),
        }
    return result


def _sample_data_stats(data_array: Any, *, quick: bool = False, crop_index_bounds: dict[str, int] | None = None) -> dict[str, Any]:
    import numpy as np

    arr = data_array
    for dim in list(getattr(arr, "dims", ())):
        lower = dim.lower()
        if crop_index_bounds and lower in {"projection_x_coordinate", "x"}:
            arr = arr.isel({dim: slice(crop_index_bounds["x_start"], crop_index_bounds["x_stop"] + 1)})
        elif crop_index_bounds and lower in {"projection_y_coordinate", "y"}:
            arr = arr.isel({dim: slice(crop_index_bounds["y_start"], crop_index_bounds["y_stop"] + 1)})
        elif quick and arr.sizes.get(dim, 1) > 80:
            arr = arr.isel({dim: slice(0, 80)})
        elif lower in {"time", "forecast_period"}:
            arr = arr.isel({dim: 0})
    values = np.asarray(arr.values, dtype=float)
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"finite_count": 0}
    return {
        "finite_count": int(finite.size),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "mean": float(np.mean(finite)),
        "shape": [int(v) for v in values.shape],
    }


def _summarize_ukv_coordinates(file_inspections: dict[str, Any], bbox: BoundingBox) -> dict[str, Any]:
    first = next(iter(file_inspections.values()), {})
    latlon = first.get("lat_lon", {})
    xy = first.get("xy", {})
    grid_mapping = first.get("grid_mapping", {})
    summary = {
        "grid_type": first.get("grid_type"),
        "lat_lon": latlon,
        "xy": xy,
        "grid_mapping": grid_mapping,
        "pyproj_transform_available": _can_build_pyproj_transform(grid_mapping),
    }
    bounds = latlon.get("bounds")
    if bounds:
        projected_index_bounds = _projected_bbox_index_bounds(bbox, xy, grid_mapping)
        fully_covered = (
            bbox.west >= bounds["west"]
            and bbox.east <= bounds["east"]
            and bbox.south >= bounds["south"]
            and bbox.north <= bounds["north"]
        )
        summary["crop_feasibility"] = {
            "bbox": bbox.__dict__,
            "source_bounds": bounds,
            "bbox_fully_covered_by_lat_lon_bounds": fully_covered,
            "approx_source_index_bounds": projected_index_bounds or "(requires source sample coordinate arrays and projection-aware crop)",
            "source_buffer_needed_for_interpolation": True,
        }
    else:
        summary["crop_feasibility"] = {
            "bbox": bbox.__dict__,
            "bbox_fully_covered_by_lat_lon_bounds": None,
            "blocker": "source lat/lon bounds not available without deeper coordinate/projection handling",
        }
    return summary


def _summarize_ukv_times(file_inspections: dict[str, Any], requested_hours: list[int]) -> dict[str, Any]:
    per_file = {field: info.get("time", {}) for field, info in file_inspections.items()}
    available = sorted(
        {
            hour
            for field_name, info in file_inspections.items()
            for hour in (_hours_from_time_summary(info.get("time", {})) or _hours_from_field_name(field_name))
        }
    )
    requested_available = all(hour in available for hour in requested_hours)
    return {
        "requested_forecast_hours": requested_hours,
        "available_forecast_hours_from_files": available,
        "missing_requested_hours": [hour for hour in requested_hours if hour not in available],
        "requested_hours_available": requested_available,
        "requested_hours_form_contiguous_sequence": requested_hours == list(range(min(requested_hours or [0]), max(requested_hours or [0]) + 1)),
        "hourly_0_to_54_proven": all(hour in available for hour in range(0, 55)) if available else False,
        "per_file": per_file,
    }


def _ukv_variable_mapping_candidates(file_inspections: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field_name, info in file_inspections.items():
        base_field = _ukv_base_field_name(field_name)
        primary = info.get("primary_data_variable")
        variables = info.get("variables", {})
        attrs = variables.get(primary, {}) if primary else {}
        spec = UKMO_UKV_REQUIRED_SOURCE_FIELDS.get(base_field, {})
        result[field_name] = {
            "field": base_field,
            "source_variable": primary,
            "units": attrs.get("units"),
            "standard_name": attrs.get("standard_name"),
            "long_name": attrs.get("long_name"),
            "intended_grib_short_name": spec.get("intended_grib_short_name"),
            "unit_conversion_required": _ukv_unit_conversion_note(field_name, attrs.get("units")),
            "confidence": spec.get("confidence", "low"),
        }
    return result


def _infer_ukv_wind_direction_convention(file_inspections: dict[str, Any]) -> dict[str, Any]:
    info = next((value for key, value in file_inspections.items() if _ukv_base_field_name(key) == "wind_direction_10m"), {})
    primary = info.get("primary_data_variable")
    attrs = info.get("variables", {}).get(primary, {}) if primary else {}
    text = " ".join(str(attrs.get(name) or "") for name in ("standard_name", "long_name", "units")).lower()
    meteorological_from = "from_direction" in text or "direction from which" in text or "wind_from_direction" in text
    return {
        "source_variable": primary,
        "units": attrs.get("units"),
        "standard_name": attrs.get("standard_name"),
        "long_name": attrs.get("long_name"),
        "is_meteorological_from_direction": meteorological_from,
        "planned_conversion_if_from": "u = -speed * sin(direction_radians); v = -speed * cos(direction_radians)",
        "status": "usable" if meteorological_from else "ambiguous",
    }


def wind_speed_direction_to_uv(speed: Any, direction_degrees: Any, *, convention: str = "from") -> tuple[Any, Any]:
    import numpy as np

    if convention != "from":
        raise ValidationError("only meteorological 'from' wind direction conversion is supported")
    radians = np.deg2rad(direction_degrees)
    u = -np.asarray(speed) * np.sin(radians)
    v = -np.asarray(speed) * np.cos(radians)
    return u, v


def _ukv_wind_uv_sample_stats(downloaded: dict[str, dict[str, Any]], file_inspections: dict[str, Any]) -> dict[str, Any] | None:
    try:
        import numpy as np
        import xarray as xr
    except ImportError:
        return None
    speed_key = next((key for key in file_inspections if _ukv_base_field_name(key) == "wind_speed_10m"), None)
    direction_key = next((key for key in file_inspections if _ukv_base_field_name(key) == "wind_direction_10m"), None)
    if speed_key is None or direction_key is None:
        return None
    speed_info = file_inspections.get(speed_key)
    direction_info = file_inspections.get(direction_key)
    if not speed_info or not direction_info:
        return None
    crop = speed_info.get("bbox_index_bounds")
    speed_var = speed_info.get("primary_data_variable")
    direction_var = direction_info.get("primary_data_variable")
    if not crop or not speed_var or not direction_var:
        return None
    with xr.open_dataset(downloaded[speed_key]["path"]) as speed_ds, xr.open_dataset(downloaded[direction_key]["path"]) as direction_ds:
        speed = speed_ds[speed_var]
        direction = direction_ds[direction_var]
        indexer: dict[str, slice] = {}
        for dim in speed.dims:
            lower = dim.lower()
            if lower in {"projection_x_coordinate", "x"}:
                indexer[dim] = slice(crop["x_start"], crop["x_stop"] + 1)
            elif lower in {"projection_y_coordinate", "y"}:
                indexer[dim] = slice(crop["y_start"], crop["y_stop"] + 1)
        speed_values = np.asarray(speed.isel(indexer).values, dtype=float)
        direction_values = np.asarray(direction.isel(indexer).values, dtype=float)
        u, v = wind_speed_direction_to_uv(speed_values, direction_values)
        return {
            "convention": "meteorological_from",
            "forecast_hour": downloaded[speed_key].get("forecast_hour"),
            "u": _array_stats(u),
            "v": _array_stats(v),
            "speed": _array_stats(speed_values),
            "direction_degrees": _array_stats(direction_values),
        }


def _ukv_base_field_name(name: str) -> str:
    return re.sub(r"_h\d{3}$", "", name)


def _ukv_regrid_sample(
    downloaded: dict[str, dict[str, Any]],
    file_inspections: dict[str, Any],
    bbox: BoundingBox,
    spacing_deg: float,
) -> dict[str, Any] | None:
    try:
        import numpy as np
        import xarray as xr
        from pyproj import CRS, Transformer
    except ImportError:
        return {"status": "blocked", "reason": "xarray, numpy, and pyproj are required for UKV regrid inspection"}

    first_key = next(iter(file_inspections), None)
    if first_key is None:
        return None
    grid_mapping = file_inspections[first_key].get("grid_mapping")
    if not grid_mapping or not grid_mapping.get("attrs"):
        return {"status": "blocked", "reason": "CF grid_mapping metadata was not available"}
    grid = build_regular_grid(bbox, spacing_deg)
    lon2d, lat2d = np.meshgrid(grid.longitudes, grid.latitudes)
    transformer = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_cf(grid_mapping["attrs"]), always_xy=True)
    x_values, y_values = transformer.transform(lon2d, lat2d)
    x_da = xr.DataArray(x_values, dims=("latitude", "longitude"))
    y_da = xr.DataArray(y_values, dims=("latitude", "longitude"))
    result: dict[str, Any] = {
        "status": "ok",
        "method": "xarray linear interpolation on projected x/y coordinates",
        "forecast_hour": 0,
        "output_grid": {
            "nx": grid.nx,
            "ny": grid.ny,
            "west": float(grid.longitudes[0]),
            "east": float(grid.longitudes[-1]),
            "south": float(grid.latitudes[0]),
            "north": float(grid.latitudes[-1]),
            "spacing_deg": spacing_deg,
        },
        "fields": {},
    }
    regridded_values: dict[str, Any] = {}
    for field_name in ("pressure_msl", "temperature_screen", "wind_speed_10m", "wind_direction_10m"):
        item_key = next((key for key, info in downloaded.items() if info.get("field") == field_name and info.get("forecast_hour") == 0), None)
        if item_key is None:
            continue
        info = file_inspections[item_key]
        var_name = info.get("primary_data_variable")
        if not var_name:
            continue
        with xr.open_dataset(downloaded[item_key]["path"]) as ds:
            arr = ds[var_name]
            try:
                interp = arr.interp(
                    projection_x_coordinate=x_da,
                    projection_y_coordinate=y_da,
                    method="linear",
                )
            except Exception as exc:
                result["fields"][field_name] = {"status": "blocked", "reason": f"linear interpolation failed: {exc}"}
                continue
            values = np.asarray(interp.values, dtype=float)
            regridded_values[field_name] = values
            result["fields"][field_name] = {
                "status": "ok",
                "stats": _array_stats(values),
                "missing_percent": _missing_percent(values),
            }
    if "wind_speed_10m" in regridded_values and "wind_direction_10m" in regridded_values:
        u, v = wind_speed_direction_to_uv(regridded_values["wind_speed_10m"], regridded_values["wind_direction_10m"])
        result["fields"]["wind_u_10m_candidate"] = {
            "status": "ok",
            "stats": _array_stats(u),
            "missing_percent": _missing_percent(u),
        }
        result["fields"]["wind_v_10m_candidate"] = {
            "status": "ok",
            "stats": _array_stats(v),
            "missing_percent": _missing_percent(v),
        }
    return result


def _regrid_ukv_source_fields(
    downloaded: dict[str, dict[str, Any]],
    file_inspections: dict[str, Any],
    bbox: BoundingBox,
    spacing_deg: float,
    forecast_hours: list[int],
) -> UKVRegriddedDataset:
    try:
        import numpy as np
        import xarray as xr
        from pyproj import CRS, Transformer
    except ImportError as exc:
        raise MissingDependencyError(
            "UKV generation requires numpy, xarray, and pyproj for projection-aware regridding."
        ) from exc

    first_key = next(iter(file_inspections), None)
    if first_key is None:
        raise ValidationError("no UKV source files were available for regridding")
    grid_mapping = file_inspections[first_key].get("grid_mapping")
    if not grid_mapping or not grid_mapping.get("attrs"):
        raise ValidationError("UKV source files do not expose usable CF grid_mapping metadata")
    xy = file_inspections[first_key].get("xy") or {}
    x_name = xy.get("x", {}).get("name") or "projection_x_coordinate"
    y_name = xy.get("y", {}).get("name") or "projection_y_coordinate"

    grid = build_regular_grid(bbox, spacing_deg)
    lon2d, lat2d = np.meshgrid(grid.longitudes, grid.latitudes)
    transformer = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_cf(grid_mapping["attrs"]), always_xy=True)
    x_values, y_values = transformer.transform(lon2d, lat2d)
    x_da = xr.DataArray(x_values, dims=("latitude", "longitude"))
    y_da = xr.DataArray(y_values, dims=("latitude", "longitude"))

    fields: dict[tuple[int, str], Any] = {}
    missing: dict[tuple[int, str], float] = {}
    for hour in forecast_hours:
        pressure = _regrid_ukv_scalar_field(downloaded, file_inspections, "pressure_msl", hour, x_name, y_name, x_da, y_da)
        temperature = _regrid_ukv_scalar_field(downloaded, file_inspections, "temperature_screen", hour, x_name, y_name, x_da, y_da)
        speed = _load_ukv_source_array(downloaded, file_inspections, "wind_speed_10m", hour)
        direction = _load_ukv_source_array(downloaded, file_inspections, "wind_direction_10m", hour)
        u_source, v_source = wind_speed_direction_to_uv(speed.values.astype(float), direction.values.astype(float))
        u_array = speed.copy(data=u_source)
        v_array = speed.copy(data=v_source)
        u = _interp_ukv_array(u_array, x_name, y_name, x_da, y_da)
        v = _interp_ukv_array(v_array, x_name, y_name, x_da, y_da)
        for short_name, values in (("prmsl", pressure), ("2t", temperature), ("10u", u), ("10v", v)):
            values = np.asarray(values, dtype=float)
            miss = _missing_percent(values)
            if miss > 0.5:
                raise ValidationError(f"UKV regridded field {short_name} f{hour:03d} has too many missing cells: {miss:.2f}%")
            fields[(hour, short_name)] = values
            missing[(hour, short_name)] = miss
    return UKVRegriddedDataset(
        grid=grid,
        forecast_hours=forecast_hours,
        fields=fields,
        source_files=downloaded,
        missing_percent=missing,
    )


def _load_ukv_source_array(
    downloaded: dict[str, dict[str, Any]],
    file_inspections: dict[str, Any],
    field_name: str,
    forecast_hour: int,
) -> Any:
    import xarray as xr

    item_key = next(
        (
            key
            for key, info in downloaded.items()
            if info.get("field") == field_name and int(info.get("forecast_hour")) == forecast_hour
        ),
        None,
    )
    if item_key is None:
        raise ValidationError(f"missing UKV source field {field_name} for f{forecast_hour:03d}")
    var_name = file_inspections[item_key].get("primary_data_variable")
    if not var_name:
        raise ValidationError(f"could not detect primary variable for UKV source field {field_name} f{forecast_hour:03d}")
    with xr.open_dataset(downloaded[item_key]["path"]) as ds:
        return ds[var_name].load()


def _regrid_ukv_scalar_field(
    downloaded: dict[str, dict[str, Any]],
    file_inspections: dict[str, Any],
    field_name: str,
    forecast_hour: int,
    x_name: str,
    y_name: str,
    x_da: Any,
    y_da: Any,
) -> Any:
    source = _load_ukv_source_array(downloaded, file_inspections, field_name, forecast_hour)
    return _interp_ukv_array(source, x_name, y_name, x_da, y_da)


def _interp_ukv_array(source: Any, x_name: str, y_name: str, x_da: Any, y_da: Any) -> Any:
    import numpy as np

    try:
        interp = source.interp({x_name: x_da, y_name: y_da}, method="linear")
    except Exception as exc:
        raise ValidationError(f"UKV projected-grid interpolation failed: {exc}") from exc
    values = np.asarray(interp.values, dtype=float)
    if values.shape != tuple(x_da.shape):
        raise ValidationError(f"UKV regridded shape mismatch: expected {tuple(x_da.shape)}, got {values.shape}")
    return values


def _write_ukv_grib2(
    dataset: UKVRegriddedDataset,
    selected_cycle: str,
    output: Path,
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    try:
        import eccodes
    except ImportError as exc:
        raise MissingDependencyError(
            "Writing UKV weather GRIB requires ECMWF ecCodes Python bindings. "
            "Install system ecCodes plus `tidal-current-grib-generator[grib]`."
        ) from exc

    reference = _datetime_from_ukv_cycle_name(selected_cycle)
    message_count = 0
    with output.open("wb") as handle:
        for hour in dataset.forecast_hours:
            for short_name in ("10u", "10v", "prmsl", "2t"):
                values = dataset.fields[(hour, short_name)]
                gid = _create_ukv_grib2_message(eccodes, dataset.grid, reference, hour, short_name, values)
                try:
                    eccodes.codes_write(gid, handle)
                finally:
                    eccodes.codes_release(gid)
                message_count += 1
            _progress(progress_callback, "wrote UKV weather forecast hour", {"hour": hour, "messages": message_count})


def _create_ukv_grib2_message(eccodes: Any, grid: Any, reference: datetime, forecast_hour: int, short_name: str, values: Any) -> Any:
    import numpy as np

    gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        eccodes.codes_set(gid, "editionNumber", 2)
        eccodes.codes_set(gid, "discipline", 0)
        eccodes.codes_set(gid, "productDefinitionTemplateNumber", 0)
        eccodes.codes_set(gid, "typeOfGeneratingProcess", 2)
        eccodes.codes_set(gid, "generatingProcessIdentifier", 255)
        eccodes.codes_set(gid, "shortName", short_name)

        eccodes.codes_set(gid, "dataDate", int(reference.strftime("%Y%m%d")))
        eccodes.codes_set(gid, "dataTime", int(reference.strftime("%H%M")))
        eccodes.codes_set(gid, "stepUnits", 1)
        eccodes.codes_set(gid, "forecastTime", int(forecast_hour))

        eccodes.codes_set(gid, "Ni", grid.nx)
        eccodes.codes_set(gid, "Nj", grid.ny)
        eccodes.codes_set(gid, "latitudeOfFirstGridPointInDegrees", float(grid.latitudes[0]))
        eccodes.codes_set(gid, "longitudeOfFirstGridPointInDegrees", float(grid.longitudes[0]))
        eccodes.codes_set(gid, "latitudeOfLastGridPointInDegrees", float(grid.latitudes[-1]))
        eccodes.codes_set(gid, "longitudeOfLastGridPointInDegrees", float(grid.longitudes[-1]))
        eccodes.codes_set(gid, "iDirectionIncrementInDegrees", float(grid.longitude_spacing_deg))
        eccodes.codes_set(gid, "jDirectionIncrementInDegrees", float(grid.latitude_spacing_deg))
        eccodes.codes_set(gid, "iScansNegatively", 0)
        eccodes.codes_set(gid, "jScansPositively", 1)
        eccodes.codes_set(gid, "jPointsAreConsecutive", 0)

        encoded = np.asarray(values, dtype=np.float64)
        if encoded.shape != grid.shape:
            raise ValidationError(f"UKV field {short_name} has shape {encoded.shape}, expected {grid.shape}")
        if np.any(~np.isfinite(encoded)):
            encoded = encoded.copy()
            encoded[~np.isfinite(encoded)] = 9999.0
            eccodes.codes_set(gid, "bitmapPresent", 1)
            eccodes.codes_set(gid, "missingValue", 9999.0)
        eccodes.codes_set(gid, "bitsPerValue", 24)
        eccodes.codes_set_values(gid, encoded.ravel(order="C"))
    except Exception:
        eccodes.codes_release(gid)
        raise
    return gid


def _read_ukv_grib_fields(path: Path) -> dict[str, Any]:
    try:
        import eccodes
        import numpy as np
    except ImportError as exc:
        raise MissingDependencyError(
            "Verifying UKV GRIB values requires ecCodes and numpy."
        ) from exc

    fields: dict[tuple[int, str], Any] = {}
    reference_time: datetime | None = None
    grid: dict[str, Any] | None = None
    with path.open("rb") as handle:
        while True:
            gid = eccodes.codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                short_name = str(eccodes.codes_get(gid, "shortName"))
                if short_name not in {"10u", "10v", "prmsl", "2t"}:
                    continue
                ni = int(eccodes.codes_get(gid, "Ni"))
                nj = int(eccodes.codes_get(gid, "Nj"))
                hour = _grib_forecast_hour(eccodes, gid)
                values = np.asarray(eccodes.codes_get_values(gid), dtype=float).reshape((nj, ni))
                fields[(hour, short_name)] = values
                if grid is None:
                    grid = {
                        "nx": ni,
                        "ny": nj,
                        "west": _normalize_longitude_180(float(eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees"))),
                        "south": float(eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees")),
                        "east": _normalize_longitude_180(float(eccodes.codes_get(gid, "longitudeOfLastGridPointInDegrees"))),
                        "north": float(eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees")),
                        "i_increment": float(eccodes.codes_get(gid, "iDirectionIncrementInDegrees")),
                        "j_increment": float(eccodes.codes_get(gid, "jDirectionIncrementInDegrees")),
                        "j_scans_positively": int(eccodes.codes_get(gid, "jScansPositively")),
                    }
                if reference_time is None:
                    data_date = f"{int(eccodes.codes_get(gid, 'dataDate')):08d}"
                    data_time = f"{int(eccodes.codes_get(gid, 'dataTime')):04d}"
                    reference_time = datetime(
                        int(data_date[0:4]),
                        int(data_date[4:6]),
                        int(data_date[6:8]),
                        int(data_time[0:2]),
                        int(data_time[2:4]),
                        tzinfo=timezone.utc,
                    )
            finally:
                eccodes.codes_release(gid)
    if reference_time is None or grid is None:
        raise ValidationError("UKV GRIB did not contain expected weather fields 10u/10v/prmsl/2t")
    return {"fields": fields, "reference_time": reference_time, "grid": grid}


def _load_copernicus_wave_dataset(
    path: Path,
    bbox: BoundingBox,
    start: datetime,
    forecast_hours: list[int],
    grid_spacing_deg: float | None,
) -> WaveRegriddedDataset:
    try:
        import numpy as np
        import xarray as xr
    except ImportError as exc:
        raise MissingDependencyError(
            "Copernicus wave NetCDF conversion requires numpy and xarray; install the netcdf/weather extras."
        ) from exc

    with xr.open_dataset(path) as ds:
        lat_name = _find_coord_name(ds, ("latitude", "lat"))
        lon_name = _find_coord_name(ds, ("longitude", "lon"))
        time_name = _find_coord_name(ds, ("time", "valid_time"))
        if lat_name is None or lon_name is None or time_name is None:
            raise ValidationError("Copernicus wave NetCDF must contain latitude, longitude, and time coordinates")
        mapping = {
            short_name: _find_data_variable(ds, aliases)
            for short_name, aliases in COPERNICUS_GLOBAL_WAVE_ALIASES.items()
        }
        missing = [short_name for short_name, var_name in mapping.items() if var_name is None]
        if missing:
            raise ValidationError(
                "Copernicus wave NetCDF is missing required variables for "
                + ", ".join(missing)
                + f"; available variables: {', '.join(ds.data_vars)}"
            )
        spacing = grid_spacing_deg or _infer_regular_coord_spacing(ds[lon_name].values, default=0.0833333)
        grid = build_regular_grid(bbox, spacing)
        fields: dict[tuple[int, str], Any] = {}
        missing_percent: dict[tuple[int, str], float] = {}
        valid_cell_count: dict[tuple[int, str], int] = {}
        for hour in forecast_hours:
            target = np.datetime64((start + timedelta(hours=hour)).replace(tzinfo=None))
            for short_name, var_name in mapping.items():
                assert var_name is not None
                try:
                    selected = ds[var_name].sel({time_name: target})
                except Exception as exc:
                    raise ValidationError(f"Copernicus wave source is missing forecast hour f{hour:03d} at {target}") from exc
                selected = _select_first_non_horizontal_dims(selected, horizontal_dims={lat_name, lon_name})
                interp = selected.interp({lat_name: grid.latitudes, lon_name: grid.longitudes}, method="linear")
                values = np.asarray(interp.values, dtype=float)
                if values.shape != grid.shape:
                    raise ValidationError(f"Copernicus wave field {short_name} f{hour:03d} has shape {values.shape}, expected {grid.shape}")
                values = _convert_wave_units(values, str(ds[var_name].attrs.get("units", "")), short_name)
                miss = _missing_percent(values)
                valid = _valid_cell_count(values)
                if valid < COPERNICUS_GLOBAL_WAVE_MIN_VALID_CELLS:
                    raise ValidationError(
                        f"Copernicus wave field {short_name} f{hour:03d} has no valid wave coverage "
                        f"inside requested bbox; missing cells: {miss:.2f}%"
                    )
                fields[(hour, short_name)] = values
                missing_percent[(hour, short_name)] = miss
                valid_cell_count[(hour, short_name)] = valid
        return WaveRegriddedDataset(
            grid=grid,
            forecast_hours=forecast_hours,
            fields=fields,
            variable_mapping={short_name: str(var_name) for short_name, var_name in mapping.items() if var_name is not None},
            missing_percent=missing_percent,
            valid_cell_count=valid_cell_count,
        )


def _write_wave_grib2(
    dataset: WaveRegriddedDataset,
    reference: datetime,
    output: Path,
    *,
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None,
) -> None:
    try:
        import eccodes
    except ImportError as exc:
        raise MissingDependencyError(
            "Writing wave GRIB requires ECMWF ecCodes Python bindings. "
            "Install system ecCodes plus `tidal-current-grib-generator[grib]`."
        ) from exc

    message_count = 0
    with output.open("wb") as handle:
        for hour in dataset.forecast_hours:
            for short_name in ("swh", "perpw", "dirpw"):
                values = dataset.fields[(hour, short_name)]
                gid = _create_ukv_grib2_message(eccodes, dataset.grid, reference, hour, short_name, values)
                try:
                    eccodes.codes_write(gid, handle)
                finally:
                    eccodes.codes_release(gid)
                message_count += 1
            _progress(
                progress_callback,
                "wrote wave forecast hour",
                {
                    "hour": hour,
                    "messages": message_count,
                    "missing_percent": {
                        short_name: dataset.missing_percent.get((hour, short_name), 0.0)
                        for short_name in ("swh", "perpw", "dirpw")
                    },
                    "valid_cell_count": {
                        short_name: dataset.valid_cell_count.get((hour, short_name), 0)
                        for short_name in ("swh", "perpw", "dirpw")
                    },
                    "missing_encoding": "GRIB2 bitmap",
                },
            )


def _find_coord_name(ds: Any, candidates: tuple[str, ...]) -> str | None:
    lower = {name.lower(): name for name in list(ds.coords) + list(ds.variables)}
    for candidate in candidates:
        if candidate.lower() in lower:
            return lower[candidate.lower()]
    return None


def _find_data_variable(ds: Any, aliases: tuple[str, ...]) -> str | None:
    lower = {name.lower(): name for name in ds.data_vars}
    for alias in aliases:
        if alias.lower() in lower:
            return lower[alias.lower()]
    for name, data_array in ds.data_vars.items():
        attrs = " ".join(str(data_array.attrs.get(key, "")) for key in ("standard_name", "long_name")).lower()
        if any(alias.lower() in attrs for alias in aliases):
            return str(name)
    return None


def _select_first_non_horizontal_dims(data_array: Any, *, horizontal_dims: set[str]) -> Any:
    indexers = {
        dim: 0
        for dim in data_array.dims
        if dim not in horizontal_dims and dim not in {"time", "valid_time"}
    }
    return data_array.isel(indexers) if indexers else data_array


def _infer_regular_coord_spacing(values: Any, *, default: float) -> float:
    import numpy as np

    values = np.asarray(values, dtype=float)
    if values.size < 2:
        return default
    diffs = np.diff(np.sort(values))
    finite = diffs[np.isfinite(diffs) & (np.abs(diffs) > 0)]
    if finite.size == 0:
        return default
    return float(np.median(np.abs(finite)))


def _convert_wave_units(values: Any, units: str, short_name: str) -> Any:
    units_normalized = units.strip().lower().replace(" ", "")
    if not units_normalized:
        return values
    if short_name == "swh":
        if units_normalized in {"m", "meter", "metre", "meters", "metres"}:
            return values
    if short_name == "perpw":
        if units_normalized in {"s", "sec", "second", "seconds"}:
            return values
    if short_name == "dirpw":
        if units_normalized in {"degree", "degrees", "degrees_true", "deg", "1"}:
            return values
        if units_normalized in {"radian", "radians", "rad"}:
            import numpy as np

            return np.degrees(values)
    raise ValidationError(f"unsupported Copernicus wave units for {short_name}: {units!r}")


def _normalize_longitude_180(value: float) -> float:
    normalized = ((value + 180.0) % 360.0) - 180.0
    return 180.0 if normalized == -180.0 and value > 0 else normalized


def _grib_forecast_hour(eccodes: Any, gid: Any) -> int:
    for key in ("forecastTime", "endStep", "stepRange"):
        try:
            value = eccodes.codes_get(gid, key)
        except Exception:
            continue
        text = str(value)
        if "-" in text:
            text = text.split("-")[-1]
        try:
            return int(float(text))
        except ValueError:
            continue
    raise ValidationError("could not determine GRIB forecast hour")


def _verify_ukv_grib_grid(grib_grid: dict[str, Any], expected_grid: Any, bbox: BoundingBox) -> dict[str, Any]:
    half_cell = max(float(expected_grid.longitude_spacing_deg), float(expected_grid.latitude_spacing_deg)) / 2.0 + 1e-9
    failures: list[str] = []
    checks = {
        "nx": int(expected_grid.nx),
        "ny": int(expected_grid.ny),
        "west": float(expected_grid.longitudes[0]),
        "east": float(expected_grid.longitudes[-1]),
        "south": float(expected_grid.latitudes[0]),
        "north": float(expected_grid.latitudes[-1]),
        "j_scans_positively": 1,
    }
    for key in ("nx", "ny"):
        if grib_grid.get(key) != checks[key]:
            failures.append(f"grid {key} mismatch: expected {checks[key]}, got {grib_grid.get(key)}")
    for key in ("west", "east", "south", "north"):
        if abs(float(grib_grid.get(key)) - checks[key]) > half_cell:
            failures.append(f"grid {key} mismatch: expected {checks[key]}, got {grib_grid.get(key)}")
    if int(grib_grid.get("j_scans_positively", -1)) != 1:
        failures.append("GRIB latitude scan direction is not south-to-north")
    if abs(checks["west"] - bbox.west) > half_cell or abs(checks["east"] - bbox.east) > half_cell:
        failures.append("GRIB longitude coverage does not match requested bbox within half a grid cell")
    if abs(checks["south"] - bbox.south) > half_cell or abs(checks["north"] - bbox.north) > half_cell:
        failures.append("GRIB latitude coverage does not match requested bbox within half a grid cell")
    return {"passed": not failures, "expected": checks, "actual": grib_grid, "failures": failures}


def _compare_arrays(expected: Any, actual: Any) -> dict[str, Any]:
    import numpy as np

    exp = np.asarray(expected, dtype=float)
    act = np.asarray(actual, dtype=float)
    if exp.shape != act.shape:
        raise ValidationError(f"array shape mismatch: expected {exp.shape}, got {act.shape}")
    mask = np.isfinite(exp) & np.isfinite(act)
    if not np.any(mask):
        raise ValidationError("no finite values available for array comparison")
    diff = act[mask] - exp[mask]
    return {
        "finite_count": int(mask.sum()),
        "max_abs_error": float(np.max(np.abs(diff))),
        "rmse": float(np.sqrt(np.mean(diff * diff))),
        "mean_bias": float(np.mean(diff)),
        "source_min": float(np.min(exp[mask])),
        "source_max": float(np.max(exp[mask])),
        "grib_min": float(np.min(act[mask])),
        "grib_max": float(np.max(act[mask])),
    }


def _missing_percent(values: Any) -> float:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return 100.0
    return float(100.0 * np.count_nonzero(~np.isfinite(arr)) / arr.size)


def _valid_cell_count(values: Any) -> int:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    return int(np.count_nonzero(np.isfinite(arr)))


def _array_stats(values: Any) -> dict[str, Any]:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {"finite_count": 0, "shape": [int(v) for v in arr.shape]}
    return {
        "finite_count": int(finite.size),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
        "mean": float(np.mean(finite)),
        "shape": [int(v) for v in arr.shape],
    }


def _classify_ukv_grid(latlon: dict[str, Any], xy: dict[str, Any], grid_mapping: dict[str, Any] | None) -> str:
    lat_shape = latlon.get("latitude_shape")
    lon_shape = latlon.get("longitude_shape")
    if lat_shape and lon_shape and len(lat_shape) == 1 and len(lon_shape) == 1:
        return "regular_lat_lon_candidate"
    if lat_shape and lon_shape and lat_shape == lon_shape and len(lat_shape) == 2:
        return "projected_or_curvilinear_with_auxiliary_2d_lat_lon"
    if xy and grid_mapping:
        return "projected_xy_with_cf_grid_mapping"
    if xy:
        return "projected_xy_without_confirmed_grid_mapping"
    return "unknown"


def _can_build_pyproj_transform(grid_mapping: dict[str, Any] | None) -> bool:
    if not grid_mapping or not grid_mapping.get("attrs"):
        return False
    try:
        from pyproj import CRS
        CRS.from_cf(grid_mapping["attrs"])
    except ImportError:
        return False
    except Exception:
        return False
    return True


def _ukv_unit_conversion_note(field_name: str, units: str | None) -> str:
    normalized = (units or "").lower()
    if field_name.startswith("wind") and ("m s-1" in normalized or "m/s" in normalized):
        return "none for speed; direction conversion still required"
    if "pressure" in field_name and normalized in {"pa", "pascal", "pascals"}:
        return "none if writing GRIB pressure in Pa"
    if "temperature" in field_name and normalized in {"k", "kelvin"}:
        return "none if writing GRIB temperature in K"
    if "degree" in normalized:
        return "none for angular units; direction convention must be verified"
    return "unknown; verify before enabling UKV GRIB output"


def _finite_range(values: Any) -> tuple[float, float] | None:
    import numpy as np

    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return None
    return (float(np.min(finite)), float(np.max(finite)))


def _is_monotonic(values: Any) -> bool | None:
    import numpy as np

    arr = np.asarray(values, dtype=float).ravel()
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return None
    diff = np.diff(arr)
    return bool(np.all(diff >= 0) or np.all(diff <= 0))


def _compact_values(values: Any, limit: int = 8) -> list[str]:
    import numpy as np

    arr = np.asarray(values).ravel()
    if arr.size <= limit:
        return [str(value) for value in arr]
    head = [str(value) for value in arr[: limit // 2]]
    tail = [str(value) for value in arr[-(limit // 2) :]]
    return head + ["..."] + tail


def _bbox_intersects_latlon_bounds(bbox: BoundingBox, bounds: dict[str, float]) -> bool:
    return not (
        bbox.east < bounds["west"]
        or bbox.west > bounds["east"]
        or bbox.north < bounds["south"]
        or bbox.south > bounds["north"]
    )


def _hours_from_time_summary(time_summary: dict[str, Any]) -> list[int]:
    hours: set[int] = set()
    for info in time_summary.values():
        units = str(info.get("units") or "").lower()
        for text in info.get("values", []):
            match = re.search(r"(?<!\d)(\d{1,3})(?:\s*)h", text, flags=re.IGNORECASE)
            if match:
                hours.add(int(match.group(1)))
                continue
            if units == "seconds":
                try:
                    seconds = float(text)
                except ValueError:
                    continue
                if seconds % 3600 == 0:
                    hours.add(int(seconds // 3600))
    return sorted(hours)


def _hours_from_field_name(field_name: str) -> list[int]:
    match = re.search(r"_h(\d{3})$", field_name)
    return [int(match.group(1))] if match else []


def _projected_xy_latlon_bounds(xy: dict[str, Any], grid_mapping: dict[str, Any] | None) -> dict[str, float] | None:
    if not xy or not grid_mapping or not grid_mapping.get("attrs"):
        return None
    try:
        import numpy as np
        from pyproj import CRS, Transformer

        x_range = xy.get("x", {}).get("range")
        y_range = xy.get("y", {}).get("range")
        if not x_range or not y_range:
            return None
        crs = CRS.from_cf(grid_mapping["attrs"])
        transformer = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
        x_values = [float(x_range[0]), float(x_range[1]), float(x_range[1]), float(x_range[0])]
        y_values = [float(y_range[0]), float(y_range[0]), float(y_range[1]), float(y_range[1])]
        lon, lat = transformer.transform(x_values, y_values)
        return {
            "west": float(np.nanmin(lon)),
            "south": float(np.nanmin(lat)),
            "east": float(np.nanmax(lon)),
            "north": float(np.nanmax(lat)),
        }
    except Exception:
        return None


def _projected_bbox_index_bounds(bbox: BoundingBox, xy: dict[str, Any], grid_mapping: dict[str, Any] | None) -> dict[str, int] | None:
    if not xy or not grid_mapping or not grid_mapping.get("attrs"):
        return None
    try:
        import numpy as np
        from pyproj import CRS, Transformer

        x_range = xy.get("x", {}).get("range")
        y_range = xy.get("y", {}).get("range")
        x_shape = xy.get("x", {}).get("shape")
        y_shape = xy.get("y", {}).get("shape")
        if not x_range or not y_range or not x_shape or not y_shape:
            return None
        crs = CRS.from_cf(grid_mapping["attrs"])
        transformer = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True)
        lon_values = [bbox.west, bbox.east, bbox.east, bbox.west]
        lat_values = [bbox.south, bbox.south, bbox.north, bbox.north]
        x_projected, y_projected = transformer.transform(lon_values, lat_values)
        x0, x1 = float(x_range[0]), float(x_range[1])
        y0, y1 = float(y_range[0]), float(y_range[1])
        nx, ny = int(x_shape[0]), int(y_shape[0])
        ix0 = int(np.floor((min(x_projected) - x0) / ((x1 - x0) / max(nx - 1, 1))))
        ix1 = int(np.ceil((max(x_projected) - x0) / ((x1 - x0) / max(nx - 1, 1))))
        iy0 = int(np.floor((min(y_projected) - y0) / ((y1 - y0) / max(ny - 1, 1))))
        iy1 = int(np.ceil((max(y_projected) - y0) / ((y1 - y0) / max(ny - 1, 1))))
        return {
            "x_start": max(0, ix0),
            "x_stop": min(nx - 1, ix1),
            "y_start": max(0, iy0),
            "y_stop": min(ny - 1, iy1),
            "estimated_points": max(0, min(nx - 1, ix1) - max(0, ix0) + 1)
            * max(0, min(ny - 1, iy1) - max(0, iy0) + 1),
        }
    except Exception:
        return None


def forecast_hour_sequence(hours: int, step_hours: int) -> list[int]:
    if hours < 0:
        raise ValidationError("--hours must be zero or greater")
    if step_hours <= 0:
        raise ValidationError("--step-hours must be greater than zero")
    if hours % step_hours != 0:
        raise ValidationError("--hours must be evenly divisible by --step-hours")
    return list(range(0, hours + 1, step_hours))


def _copernicus_wave_time_window(
    requested_start: datetime,
    requested_end: datetime,
    step_hours: int,
) -> tuple[datetime, datetime, list[int]]:
    wave_start = _ceil_datetime_to_hour_cadence(requested_start, step_hours)
    wave_end = _floor_datetime_to_hour_cadence(requested_end, step_hours)
    if wave_start > wave_end:
        raise ValidationError(
            "requested time window contains no Copernicus Global Waves valid times "
            f"on the {step_hours}-hour cadence"
        )
    duration_hours = int((wave_end - wave_start).total_seconds() // 3600)
    return wave_start, wave_end, forecast_hour_sequence(duration_hours, step_hours)


def _ceil_datetime_to_hour_cadence(value: datetime, step_hours: int) -> datetime:
    base = value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if value.astimezone(timezone.utc) != base:
        base += timedelta(hours=1)
    remainder = base.hour % step_hours
    if remainder:
        base += timedelta(hours=step_hours - remainder)
    return base


def _floor_datetime_to_hour_cadence(value: datetime, step_hours: int) -> datetime:
    base = value.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    remainder = base.hour % step_hours
    if remainder:
        base -= timedelta(hours=remainder)
    return base


def _wave_valid_time_strings(reference: datetime, forecast_hours: list[int]) -> list[str]:
    return [
        (reference + timedelta(hours=hour)).isoformat().replace("+00:00", "Z")
        for hour in forecast_hours
    ]


def ukmo_ukv_forecast_hour_sequence(hours: int, step_hours: int) -> list[int]:
    if hours < 0:
        raise ValidationError("--hours must be zero or greater")
    if step_hours <= 0:
        raise ValidationError("--step-hours must be greater than zero")
    if hours > 120:
        raise ValidationError("UKV forecasts are supported only to 120 hours")
    if step_hours == 1:
        if hours <= 54:
            return list(range(0, hours + 1))
        return list(range(0, 55)) + list(range(57, hours + 1, 3))
    if step_hours == 3:
        if hours % 3 != 0:
            raise ValidationError("--hours must be evenly divisible by --step-hours")
        return list(range(0, hours + 1, 3))
    raise ValidationError("--step-hours must be 1 or 3 for UKV")


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
        if request.cycle not in {"00", "03", "06", "09", "12", "15", "18", "21"}:
            raise ValidationError("--cycle must be auto, 00, 03, 06, 09, 12, 15, 18, or 21")
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


def _validate_copernicus_global_wave_request(request: CopernicusGlobalWaveRequest) -> None:
    if not request.username:
        raise ValidationError("Copernicus username is required for Copernicus Global Waves")
    if not request.password:
        raise ValidationError("Copernicus password is required for Copernicus Global Waves")
    if request.step_hours != 3:
        raise ValidationError("Copernicus Global Waves currently supports 3-hour wave steps")
    if request.hours > 240:
        raise ValidationError("Copernicus Global Waves forecast requests are limited to 240 hours")
    if request.grid_spacing_deg is not None and request.grid_spacing_deg <= 0:
        raise ValidationError("--weather-grid-spacing-deg must be greater than zero")
    forecast_hour_sequence(request.hours, request.step_hours)


def _validate_ecmwf_request(request: ECMWFWeatherRequest) -> None:
    if request.step_hours not in {3, 6, 12}:
        raise ValidationError("--step-hours must be one of 3, 6, or 12 for ECMWF Open Data")
    if request.cycle != "auto":
        if request.cycle not in {"00", "03", "06", "09", "12", "15", "18", "21"}:
            raise ValidationError("--cycle must be auto, 00, 03, 06, 09, 12, 15, 18, or 21")
        if not request.date:
            raise ValidationError("--date YYYYMMDD is required when --cycle is explicit")
        _validate_date(request.date)
    forecast_hour_sequence(request.hours, request.step_hours)


def _validate_ukmo_ukv_request(request: UKMOUKVWeatherRequest) -> None:
    if request.bbox.west < UKMO_UKV_DOMAIN.west or request.bbox.east > UKMO_UKV_DOMAIN.east:
        raise ValidationError("UKV bbox is outside the supported UK/Ireland regional domain")
    if request.bbox.south < UKMO_UKV_DOMAIN.south or request.bbox.north > UKMO_UKV_DOMAIN.north:
        raise ValidationError("UKV bbox is outside the supported UK/Ireland regional domain")
    ukmo_ukv_forecast_hour_sequence(request.hours, request.step_hours)
    if request.weather_grid_spacing_deg <= 0:
        raise ValidationError("--weather-grid-spacing-deg must be greater than zero")
    if request.cycle != "auto":
        if request.cycle not in {"00", "03", "06", "09", "12", "15", "18", "21"}:
            raise ValidationError("--cycle must be auto, 00, 03, 06, 09, 12, 15, 18, or 21")
        if not request.date:
            raise ValidationError("--date YYYYMMDD is required when --cycle is explicit")
        _validate_date(request.date)
    gfs_variables_for_preset(request.preset)


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


def _datetime_from_ukv_cycle_name(cycle_name: str) -> datetime:
    return datetime.strptime(cycle_name, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)


def _weather_cycle_from_ukv_cycle_name(cycle_name: str) -> WeatherCycle:
    dt = _datetime_from_ukv_cycle_name(cycle_name)
    return WeatherCycle(dt.strftime("%Y%m%d"), f"{dt.hour:02d}")


def _validate_date(value: str) -> None:
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValidationError("--date must use YYYYMMDD format") from exc


def _progress(callback: Callable[[str, dict[str, Any]], None] | None, step: str, details: dict[str, Any]) -> None:
    if callback is not None:
        callback(step, details)
