"""GRIB writer backends."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError
from tidal_current_grib_generator.model import CurrentGrid


def _import_eccodes():
    try:
        import eccodes
    except ImportError as exc:
        raise MissingDependencyError(
            "Writing GRIB requires ECMWF ecCodes Python bindings. "
            "Install system ecCodes plus the optional Python dependency, e.g. "
            "`pip install tidal-current-grib-generator[grib]`."
        ) from exc
    return eccodes


@dataclass(frozen=True)
class GribWriteSummary:
    message_count: int
    output: Path


class EccodesGrib1CurrentWriter:
    """Write GRIB1 u/v current-component messages compatible with OpenCPN.

    OpenCPN grib_pi has historically recognised GRIB1 current components encoded
    as parameter 49 (eastward/u current) and 50 (northward/v current) in metres
    per second on a regular latitude/longitude surface field. This mirrors common
    current GRIB examples seen in OpenCPN testing. The writer keeps all values in
    SI units; CLI ``--units`` applies only to source input conventions.
    """

    U_CURRENT_PARAM = 49
    V_CURRENT_PARAM = 50
    MISSING_VALUE = 9999.0

    def write(
        self,
        grids: Iterable[CurrentGrid],
        output: Path,
        progress_callback: Callable[[int, CurrentGrid], None] | None = None,
    ) -> GribWriteSummary:
        eccodes = _import_eccodes()
        output = output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        grid_iter = iter(grids)
        first_grid = next(grid_iter, None)
        if first_grid is None:
            raise ValidationError("no current grids were provided for GRIB writing")
        reference_time = self._reference_time(first_grid.time)

        message_count = 0
        with output.open("wb") as handle:
            for current in _with_first(first_grid, grid_iter):
                for parameter, values in (
                    (self.U_CURRENT_PARAM, current.u_mps),
                    (self.V_CURRENT_PARAM, current.v_mps),
                ):
                    gid = self._create_message(eccodes, current, reference_time, parameter, values)
                    try:
                        eccodes.codes_write(gid, handle)
                    finally:
                        eccodes.codes_release(gid)
                    message_count += 1
                if progress_callback is not None:
                    progress_callback(message_count, current)
        return GribWriteSummary(message_count=message_count, output=output)

    def _create_message(
        self,
        eccodes,
        current: CurrentGrid,
        reference_time: datetime,
        parameter: int,
        values: np.ndarray,
    ):
        grid = current.grid
        if grid.nx < 2 or grid.ny < 2:
            raise ValidationError("GRIB output requires at least a 2 x 2 grid")

        gid = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib1")
        try:
            eccodes.codes_set(gid, "editionNumber", 1)
            eccodes.codes_set(gid, "table2Version", 2)
            eccodes.codes_set(gid, "indicatorOfParameter", parameter)
            eccodes.codes_set(gid, "indicatorOfTypeOfLevel", 1)
            eccodes.codes_set(gid, "level", 0)

            valid_time = current.time.astimezone(timezone.utc)
            forecast_hours = int((valid_time - reference_time).total_seconds() // 3600)
            if forecast_hours < 0:
                raise ValidationError("current grids must not be earlier than the GRIB reference time")
            eccodes.codes_set(gid, "dataDate", int(reference_time.strftime("%Y%m%d")))
            eccodes.codes_set(gid, "dataTime", int(reference_time.strftime("%H%M")))
            eccodes.codes_set(gid, "indicatorOfUnitOfTimeRange", 1)
            eccodes.codes_set(gid, "P1", forecast_hours)
            eccodes.codes_set(gid, "P2", 0)
            eccodes.codes_set(gid, "timeRangeIndicator", 0)

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
            if current.mask is not None:
                encoded = encoded.copy()
                encoded[current.mask] = self.MISSING_VALUE
                eccodes.codes_set(gid, "bitmapPresent", 1)
                eccodes.codes_set(gid, "missingValue", self.MISSING_VALUE)
            eccodes.codes_set(gid, "bitsPerValue", 16)
            eccodes.codes_set_values(gid, encoded.ravel(order="C"))
        except Exception:
            eccodes.codes_release(gid)
            raise
        return gid

    @staticmethod
    def _reference_time(valid_time: datetime) -> datetime:
        return valid_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _with_first(first: CurrentGrid, rest: Iterable[CurrentGrid]) -> Iterable[CurrentGrid]:
    yield first
    yield from rest
