"""Current source registry."""

from __future__ import annotations

from pathlib import Path

from tidal_current_grib_generator.errors import UnsupportedSourceError, ValidationError
from tidal_current_grib_generator.sources.base import CurrentSource
from tidal_current_grib_generator.sources.netcdf import NetCDFCurrentSource
from tidal_current_grib_generator.sources.pytmd import PyTMDSource, PyTMDTPXOSource
from tidal_current_grib_generator.sources.synthetic import ConstantCurrentSource, SyntheticRotaryTideSource


def create_source(
    name: str,
    units: str = "mps",
    model_directory: Path | None = None,
    model_name: str = "TPXO10-atlas-v2-nc",
    definition_file: Path | None = None,
    input_netcdf: Path | None = None,
    u_variable: str | None = None,
    v_variable: str | None = None,
    lat_variable: str | None = None,
    lon_variable: str | None = None,
    time_variable: str | None = None,
    depth_index: int | None = None,
    depth_value: float | None = None,
    assume_units: str | None = None,
    nearest_time: bool = False,
) -> CurrentSource:
    normalized = name.strip().lower()
    if normalized == "synthetic":
        return SyntheticRotaryTideSource()
    if normalized == "constant":
        return ConstantCurrentSource(u=1.0, v=0.0, units=units)
    if normalized in {"pytmd", "tpxo"}:
        if model_directory is None:
            raise ValidationError("--model-dir is required for the TPXO/pyTMD source")
        return PyTMDTPXOSource(
            model_directory=model_directory,
            model_name=model_name,
            definition_file=definition_file,
        )
    if normalized in {"netcdf", "copernicus"}:
        if input_netcdf is None:
            raise ValidationError("--input-netcdf is required for the NetCDF source")
        return NetCDFCurrentSource(
            input_netcdf=input_netcdf,
            u_variable=u_variable,
            v_variable=v_variable,
            lat_variable=lat_variable,
            lon_variable=lon_variable,
            time_variable=time_variable,
            depth_index=depth_index,
            depth_value=depth_value,
            assume_units=assume_units,
            nearest_time=nearest_time,
        )
    raise UnsupportedSourceError(f"unsupported source {name!r}; choose synthetic, constant, tpxo, or netcdf")


__all__ = [
    "ConstantCurrentSource",
    "CurrentSource",
    "NetCDFCurrentSource",
    "PyTMDSource",
    "PyTMDTPXOSource",
    "SyntheticRotaryTideSource",
    "create_source",
]
