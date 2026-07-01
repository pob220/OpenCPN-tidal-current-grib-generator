"""Internal data structures for current fields."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from .errors import ValidationError
from .geo import RegularGrid

KNOT_TO_MPS = 0.514444
MPS_TO_KNOT = 1.0 / KNOT_TO_MPS


@dataclass(frozen=True)
class CurrentGrid:
    """Eastward/northward current components on a regular grid, in metres per second."""

    time: datetime
    grid: RegularGrid
    u_mps: np.ndarray
    v_mps: np.ndarray
    mask: np.ndarray | None = None

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise ValidationError("current grid time must be timezone-aware")
        object.__setattr__(self, "time", self.time.astimezone(timezone.utc))
        if self.u_mps.shape != self.grid.shape or self.v_mps.shape != self.grid.shape:
            raise ValidationError("u and v arrays must match the grid shape")
        if self.mask is not None and self.mask.shape != self.grid.shape:
            raise ValidationError("mask must match the grid shape")


def components_to_speed_direction(u_mps: float, v_mps: float) -> tuple[float, float]:
    """Convert components to speed in knots and direction-toward in degrees true."""

    speed_knots = float((u_mps * u_mps + v_mps * v_mps) ** 0.5 * MPS_TO_KNOT)
    direction = (float(np.degrees(np.arctan2(u_mps, v_mps))) + 360.0) % 360.0
    return speed_knots, direction


def direction_error_degrees(predicted: float, reference: float) -> float:
    """Return signed angular error in [-180, 180] degrees."""

    return ((predicted - reference + 180.0) % 360.0) - 180.0


def speed_direction_to_components(speed: float, direction_degrees: float, units: str = "mps") -> tuple[float, float]:
    """Convert speed/direction-toward to eastward/northward components in m/s."""

    if units == "knots":
        speed_mps = speed * KNOT_TO_MPS
    elif units == "mps":
        speed_mps = speed
    else:
        raise ValidationError("units must be 'knots' or 'mps'")
    radians = np.radians(direction_degrees)
    return float(speed_mps * np.sin(radians)), float(speed_mps * np.cos(radians))
