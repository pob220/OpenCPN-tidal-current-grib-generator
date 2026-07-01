"""GRIB stream validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tidal_current_grib_generator.errors import ValidationError


@dataclass(frozen=True)
class GribScanResult:
    message_count: int
    byte_count: int


def scan_grib_messages(path: Path) -> GribScanResult:
    """Validate that each message starts with GRIB and ends with 7777."""

    data = path.read_bytes()
    offset = 0
    count = 0
    while offset < len(data):
        if data[offset : offset + 4] != b"GRIB":
            raise ValidationError(f"GRIB marker not found at byte offset {offset}")
        if offset + 8 > len(data):
            raise ValidationError(f"truncated GRIB header at byte offset {offset}")
        edition = data[offset + 7]
        if edition == 1:
            length = int.from_bytes(data[offset + 4 : offset + 7], "big")
        elif edition == 2:
            if offset + 16 > len(data):
                raise ValidationError(f"truncated GRIB2 header at byte offset {offset}")
            length = int.from_bytes(data[offset + 8 : offset + 16], "big")
        else:
            raise ValidationError(f"unsupported GRIB edition {edition} at byte offset {offset}")
        if length <= 0 or offset + length > len(data):
            raise ValidationError(f"invalid GRIB message length {length} at byte offset {offset}")
        if data[offset + length - 4 : offset + length] != b"7777":
            raise ValidationError(f"GRIB terminator not found for message at byte offset {offset}")
        offset += length
        count += 1
    return GribScanResult(message_count=count, byte_count=len(data))


def inspect_grib(path: Path) -> dict[str, Any]:
    scan = scan_grib_messages(path)
    data = path.read_bytes()
    offset = 0
    edition_counts: dict[int, int] = {}
    while offset < len(data):
        edition = data[offset + 7]
        edition_counts[edition] = edition_counts.get(edition, 0) + 1
        length = int.from_bytes(data[offset + 4 : offset + 7], "big") if edition == 1 else int.from_bytes(data[offset + 8 : offset + 16], "big")
        offset += length
    result: dict[str, Any] = {
        "path": str(path),
        "message_count": scan.message_count,
        "byte_count": scan.byte_count,
        "edition_counts": edition_counts,
        "stream_valid": True,
    }
    try:
        import eccodes
    except ImportError:
        result["eccodes_available"] = False
        result["current_component_counts"] = {"u_49": edition_counts.get(1, 0) if False else 0, "v_50": 0}
        return result

    result["eccodes_available"] = True
    parameter_counts: dict[str, int] = {}
    parameter_names: dict[str, str] = {}
    current_counts = {"u_49": 0, "v_50": 0}
    valid_times: list[str] = []
    coverages: list[dict[str, float]] = []
    with path.open("rb") as handle:
        while True:
            gid = eccodes.codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                parameter = _codes_get(eccodes, gid, "indicatorOfParameter")
                if parameter is None:
                    parameter = _codes_get(eccodes, gid, "parameterNumber")
                if parameter is not None:
                    key = str(parameter)
                    parameter_counts[key] = parameter_counts.get(key, 0) + 1
                    name = _codes_get(eccodes, gid, "parameterName") or _codes_get(eccodes, gid, "shortName")
                    if name:
                        parameter_names[key] = str(name)
                    if int(parameter) == 49:
                        current_counts["u_49"] += 1
                    if int(parameter) == 50:
                        current_counts["v_50"] += 1
                valid_time = _valid_time(eccodes, gid)
                if valid_time:
                    valid_times.append(valid_time)
                coverage = _coverage(eccodes, gid)
                if coverage:
                    coverages.append(coverage)
            finally:
                eccodes.codes_release(gid)
    result["parameter_counts"] = parameter_counts
    result["parameter_names"] = parameter_names
    result["current_component_counts"] = current_counts
    if valid_times:
        result["first_valid_time"] = min(valid_times)
        result["last_valid_time"] = max(valid_times)
    if coverages:
        result["coverage"] = coverages[0]
    return result


def _codes_get(eccodes: Any, gid: int, key: str) -> Any:
    try:
        return eccodes.codes_get(gid, key)
    except Exception:
        return None


def _valid_time(eccodes: Any, gid: int) -> str | None:
    date = _codes_get(eccodes, gid, "validityDate")
    time = _codes_get(eccodes, gid, "validityTime")
    if date is None or time is None:
        return None
    return f"{int(date):08d}T{int(time):04d}"


def _coverage(eccodes: Any, gid: int) -> dict[str, float] | None:
    keys = {
        "west": "longitudeOfFirstGridPointInDegrees",
        "south": "latitudeOfFirstGridPointInDegrees",
        "east": "longitudeOfLastGridPointInDegrees",
        "north": "latitudeOfLastGridPointInDegrees",
    }
    values = {label: _codes_get(eccodes, gid, key) for label, key in keys.items()}
    if any(value is None for value in values.values()):
        return None
    return {label: float(value) for label, value in values.items()}
