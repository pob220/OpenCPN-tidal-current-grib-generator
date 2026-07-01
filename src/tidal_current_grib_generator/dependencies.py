"""Runtime dependency checks for CLI/plugin diagnostics."""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DependencyStatus:
    python: bool
    tidal_current_grib: bool
    eccodes: bool
    xarray: bool
    netcdf4: bool
    copernicusmarine: bool
    tidal_current_grib_executable: str | None
    writable_output_directory: bool

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def check_dependencies(output_directory: Path | None = None) -> DependencyStatus:
    writable = False
    if output_directory is not None:
        try:
            output_directory.expanduser().mkdir(parents=True, exist_ok=True)
            test_path = output_directory.expanduser() / ".currentgrib_write_test"
            test_path.write_text("ok")
            test_path.unlink()
            writable = True
        except OSError:
            writable = False
    return DependencyStatus(
        python=True,
        tidal_current_grib=importlib.util.find_spec("tidal_current_grib_generator") is not None,
        eccodes=importlib.util.find_spec("eccodes") is not None,
        xarray=importlib.util.find_spec("xarray") is not None,
        netcdf4=importlib.util.find_spec("netCDF4") is not None,
        copernicusmarine=importlib.util.find_spec("copernicusmarine") is not None,
        tidal_current_grib_executable=shutil.which("tidal-current-grib"),
        writable_output_directory=writable,
    )
