"""pyTMD/TPXO source placeholder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tidal_current_grib_generator.errors import MissingDependencyError
from tidal_current_grib_generator.geo import BoundingBox, RegularGrid
from tidal_current_grib_generator.model import CurrentGrid
from tidal_current_grib_generator.sources.base import CurrentSource, SourceDescription


@dataclass(frozen=True)
class PyTMDSource(CurrentSource):
    """Skeleton for a future pyTMD-backed tidal-current source."""

    model_directory: Path
    model_name: str = "TPXO10-atlas"

    def describe(self) -> SourceDescription:
        return SourceDescription(
            name="pytmd",
            summary="pyTMD/TPXO tidal-current adapter skeleton.",
            data_notice="Users must obtain model data separately under suitable licence terms.",
        )

    def get_current_grid(self, bbox: BoundingBox, time: datetime, grid: RegularGrid) -> CurrentGrid:
        try:
            import pyTMD  # noqa: F401
        except ImportError as exc:
            raise MissingDependencyError(
                "pyTMD is not installed. Install the optional 'pytmd' extra and provide licensed model files."
            ) from exc
        raise NotImplementedError(
            "PyTMDSource is a documented skeleton in this first release. "
            "See docs/tpxo_pytmd_notes.md for the intended implementation."
        )
