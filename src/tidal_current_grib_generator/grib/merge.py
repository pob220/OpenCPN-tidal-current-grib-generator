"""GRIB stream merge helpers."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tidal_current_grib_generator.errors import ValidationError
from tidal_current_grib_generator.grib.validation import inspect_grib, scan_grib_messages


@dataclass(frozen=True)
class MergeGribsResult:
    current: Path
    weather: Path
    output: Path
    current_message_count: int
    weather_message_count: int
    output_message_count: int
    byte_count: int
    inspection: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "current": str(self.current),
            "weather": str(self.weather),
            "output": str(self.output),
            "current_message_count": self.current_message_count,
            "weather_message_count": self.weather_message_count,
            "output_message_count": self.output_message_count,
            "byte_count": self.byte_count,
            "inspection": self.inspection,
        }


def merge_grib_files(current: Path, weather: Path, output: Path, *, overwrite: bool = False) -> MergeGribsResult:
    current = current.expanduser()
    weather = weather.expanduser()
    output = output.expanduser()
    if not current.exists():
        raise ValidationError(f"current GRIB not found: {current}")
    if not weather.exists():
        raise ValidationError(f"weather GRIB not found: {weather}")
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")
    if output.exists() and not overwrite:
        raise ValidationError(f"output already exists: {output}; use --overwrite to replace it")

    current_scan = scan_grib_messages(current)
    weather_scan = scan_grib_messages(weather)
    expected_messages = current_scan.message_count + weather_scan.message_count
    tmp_path: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            with current.open("rb") as current_handle:
                tmp.write(current_handle.read())
            with weather.open("rb") as weather_handle:
                tmp.write(weather_handle.read())
        output_scan = scan_grib_messages(tmp_path)
        if output_scan.message_count != expected_messages:
            raise ValidationError(
                f"merged GRIB contains {output_scan.message_count} messages, expected {expected_messages}"
            )
        inspection = inspect_grib(tmp_path)
        os.replace(tmp_path, output)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
    return MergeGribsResult(
        current=current,
        weather=weather,
        output=output,
        current_message_count=current_scan.message_count,
        weather_message_count=weather_scan.message_count,
        output_message_count=output_scan.message_count,
        byte_count=output_scan.byte_count,
        inspection=inspection,
    )
