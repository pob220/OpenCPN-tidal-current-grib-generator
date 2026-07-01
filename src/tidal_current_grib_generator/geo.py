"""Geographic and temporal validation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np

from .errors import ValidationError


@dataclass(frozen=True)
class BoundingBox:
    """Longitude/latitude bounding box in degrees."""

    west: float
    south: float
    east: float
    north: float

    @classmethod
    def from_values(cls, values: list[float] | tuple[float, float, float, float]) -> "BoundingBox":
        if len(values) != 4:
            raise ValidationError("bbox must contain four values: west south east north")
        bbox = cls(float(values[0]), float(values[1]), float(values[2]), float(values[3]))
        bbox.validate()
        return bbox

    def validate(self) -> None:
        if not (-180.0 <= self.west <= 180.0 and -180.0 <= self.east <= 180.0):
            raise ValidationError("bbox longitudes must be within [-180, 180]")
        if not (-90.0 <= self.south <= 90.0 and -90.0 <= self.north <= 90.0):
            raise ValidationError("bbox latitudes must be within [-90, 90]")
        if self.west >= self.east:
            raise ValidationError("bbox west must be less than east; antimeridian boxes are not supported yet")
        if self.south >= self.north:
            raise ValidationError("bbox south must be less than north")


@dataclass(frozen=True)
class RegularGrid:
    """Regular latitude/longitude grid."""

    latitudes: np.ndarray
    longitudes: np.ndarray
    spacing_deg: float

    @property
    def ny(self) -> int:
        return int(self.latitudes.size)

    @property
    def nx(self) -> int:
        return int(self.longitudes.size)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.ny, self.nx)


def parse_utc_datetime(value: str) -> datetime:
    """Parse an ISO timestamp and return a UTC-aware datetime."""

    raw = value.strip()
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError(f"invalid datetime {value!r}; use ISO-8601, e.g. 2026-07-01T00:00:00Z") from exc
    if parsed.tzinfo is None:
        raise ValidationError("datetime must include a timezone; UTC 'Z' is recommended")
    return parsed.astimezone(timezone.utc)


def build_regular_grid(bbox: BoundingBox, spacing_deg: float) -> RegularGrid:
    """Build an inclusive regular grid for a bbox."""

    if spacing_deg <= 0.0:
        raise ValidationError("grid spacing must be greater than zero")
    width = bbox.east - bbox.west
    height = bbox.north - bbox.south
    tolerance = max(1e-12, spacing_deg * 1e-9)
    if spacing_deg > width + tolerance or spacing_deg > height + tolerance:
        raise ValidationError("grid spacing must be smaller than both bbox width and height")

    nx = int(round(width / spacing_deg)) + 1
    ny = int(round(height / spacing_deg)) + 1
    if nx < 2 or ny < 2:
        raise ValidationError("grid must contain at least two points in each dimension")
    if nx * ny > 5_000_000:
        raise ValidationError(f"grid is too large ({nx} x {ny}); reduce bbox or increase spacing")

    longitudes = bbox.west + np.arange(nx, dtype=float) * spacing_deg
    latitudes = bbox.south + np.arange(ny, dtype=float) * spacing_deg
    longitudes[-1] = bbox.east
    latitudes[-1] = bbox.north
    return RegularGrid(latitudes=latitudes, longitudes=longitudes, spacing_deg=spacing_deg)


def build_time_sequence(start: datetime, hours: int, step_hours: int) -> list[datetime]:
    """Return forecast-valid times including start and final step within the requested duration."""

    if start.tzinfo is None:
        raise ValidationError("start time must be timezone-aware")
    start = start.astimezone(timezone.utc)
    if hours < 0:
        raise ValidationError("hours must be zero or greater")
    if step_hours <= 0:
        raise ValidationError("step-hours must be greater than zero")
    if hours % step_hours != 0:
        raise ValidationError("hours must be evenly divisible by step-hours")
    count = hours // step_hours
    return [start + timedelta(hours=i * step_hours) for i in range(count + 1)]
