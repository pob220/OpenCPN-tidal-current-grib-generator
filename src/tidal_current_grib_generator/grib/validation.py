"""GRIB stream validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

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
