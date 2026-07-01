"""Current-source abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from tidal_current_grib_generator.geo import BoundingBox, RegularGrid
from tidal_current_grib_generator.model import CurrentGrid


@dataclass(frozen=True)
class SourceDescription:
    name: str
    summary: str
    data_notice: str


class CurrentSource(ABC):
    """Interface implemented by tidal/ocean-current model adapters."""

    @abstractmethod
    def describe(self) -> SourceDescription:
        """Return human-readable source metadata."""

    def supports_bbox(self, bbox: BoundingBox) -> bool:
        return bool(bbox)

    def supports_time_range(self, start: datetime, end: datetime) -> bool:
        return bool(start <= end)

    @abstractmethod
    def get_current_grid(self, bbox: BoundingBox, time: datetime, grid: RegularGrid) -> CurrentGrid:
        """Return u/v current components in metres per second."""
