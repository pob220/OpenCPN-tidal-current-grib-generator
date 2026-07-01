"""Small GRIB value reader for validation commands."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from tidal_current_grib_generator.errors import MissingDependencyError, ValidationError


def sample_current_components(path: Path, lat: float, lon: float, time: datetime) -> tuple[float, float]:
    try:
        import eccodes
    except ImportError as exc:
        raise MissingDependencyError(
            "Reading GRIB values requires ecCodes. Install `tidal-current-grib-generator[grib]`."
        ) from exc

    target = time.astimezone(timezone.utc)
    components: dict[int, float] = {}
    with path.open("rb") as handle:
        while True:
            gid = eccodes.codes_grib_new_from_file(handle)
            if gid is None:
                break
            try:
                parameter = _codes_get(eccodes, gid, "indicatorOfParameter")
                if parameter not in {49, 50}:
                    continue
                if _valid_time(eccodes, gid) != target:
                    continue
                components[int(parameter)] = _nearest_value(eccodes, gid, lat, lon)
            finally:
                eccodes.codes_release(gid)
    if 49 not in components or 50 not in components:
        raise ValidationError(
            f"could not find both GRIB1 current components 49 and 50 for {target.isoformat()}"
        )
    return components[49], components[50]


def _nearest_value(eccodes: Any, gid: int, lat: float, lon: float) -> float:
    ni = int(eccodes.codes_get(gid, "Ni"))
    nj = int(eccodes.codes_get(gid, "Nj"))
    lat0 = float(eccodes.codes_get(gid, "latitudeOfFirstGridPointInDegrees"))
    lon0 = float(eccodes.codes_get(gid, "longitudeOfFirstGridPointInDegrees"))
    lat1 = float(eccodes.codes_get(gid, "latitudeOfLastGridPointInDegrees"))
    lon1 = float(eccodes.codes_get(gid, "longitudeOfLastGridPointInDegrees"))
    lats = np.linspace(lat0, lat1, nj)
    lons = np.linspace(lon0, lon1, ni)
    iy = int(np.argmin(np.abs(lats - lat)))
    ix = int(np.argmin(np.abs(lons - lon)))
    values = np.asarray(eccodes.codes_get_values(gid), dtype=float).reshape((nj, ni))
    return float(values[iy, ix])


def _valid_time(eccodes: Any, gid: int) -> datetime | None:
    date = _codes_get(eccodes, gid, "validityDate")
    time = _codes_get(eccodes, gid, "validityTime")
    if date is None or time is None:
        return None
    date_text = f"{int(date):08d}"
    time_text = f"{int(time):04d}"
    return datetime(
        int(date_text[0:4]),
        int(date_text[4:6]),
        int(date_text[6:8]),
        int(time_text[0:2]),
        int(time_text[2:4]),
        tzinfo=timezone.utc,
    )


def _codes_get(eccodes: Any, gid: int, key: str) -> Any:
    try:
        return eccodes.codes_get(gid, key)
    except Exception:
        return None
