"""Built-in current sources used for testing and development."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from tidal_current_grib_generator.geo import BoundingBox, RegularGrid
from tidal_current_grib_generator.model import CurrentGrid, KNOT_TO_MPS
from tidal_current_grib_generator.sources.base import CurrentSource, SourceDescription


@dataclass(frozen=True)
class ConstantCurrentSource(CurrentSource):
    """Constant current source, useful for tests and calibration fixtures."""

    u: float = 0.0
    v: float = 0.0
    units: str = "mps"

    def describe(self) -> SourceDescription:
        return SourceDescription(
            name="constant",
            summary="Constant u/v current over the full grid.",
            data_notice="Generated test data; not suitable for navigation.",
        )

    def get_current_grid(self, bbox: BoundingBox, time: datetime, grid: RegularGrid) -> CurrentGrid:
        multiplier = KNOT_TO_MPS if self.units == "knots" else 1.0
        u = np.full(grid.shape, self.u * multiplier, dtype=np.float64)
        v = np.full(grid.shape, self.v * multiplier, dtype=np.float64)
        return CurrentGrid(time=time.astimezone(timezone.utc), grid=grid, u_mps=u, v_mps=v)


@dataclass(frozen=True)
class SyntheticRotaryTideSource(CurrentSource):
    """Deterministic rotary tide-like source for GRIB and UI testing."""

    peak_speed_knots: float = 2.2
    period_hours: float = 12.42

    def describe(self) -> SourceDescription:
        return SourceDescription(
            name="synthetic",
            summary="Deterministic rotary tide-like vector field with spatial phase and amplitude gradients.",
            data_notice="Synthetic test data only; not suitable for navigation.",
        )

    def get_current_grid(self, bbox: BoundingBox, time: datetime, grid: RegularGrid) -> CurrentGrid:
        lat2d, lon2d = np.meshgrid(grid.latitudes, grid.longitudes, indexing="ij")
        lon_norm = (lon2d - bbox.west) / max(bbox.east - bbox.west, 1e-9)
        lat_norm = (lat2d - bbox.south) / max(bbox.north - bbox.south, 1e-9)

        unix_hours = time.astimezone(timezone.utc).timestamp() / 3600.0
        temporal_phase = 2.0 * np.pi * (unix_hours / self.period_hours)
        spatial_phase = 1.4 * lon_norm - 0.9 * lat_norm

        amplitude_knots = self.peak_speed_knots * (0.35 + 0.65 * (0.25 + 0.75 * lon_norm))
        amplitude_knots *= 0.82 + 0.18 * np.cos(np.pi * (lat_norm - 0.35))
        amplitude_mps = amplitude_knots * KNOT_TO_MPS

        phase = temporal_phase + spatial_phase
        ellipticity = 0.68 + 0.18 * np.sin(np.pi * lat_norm)
        shear = 0.12 * amplitude_mps * np.sin(2.0 * np.pi * lon_norm)

        u = amplitude_mps * np.cos(phase) + shear
        v = amplitude_mps * ellipticity * np.sin(phase)
        return CurrentGrid(time=time.astimezone(timezone.utc), grid=grid, u_mps=u, v_mps=v)
