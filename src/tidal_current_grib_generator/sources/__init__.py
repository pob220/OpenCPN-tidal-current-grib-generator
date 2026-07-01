"""Current source registry."""

from __future__ import annotations

from pathlib import Path

from tidal_current_grib_generator.errors import UnsupportedSourceError, ValidationError
from tidal_current_grib_generator.sources.base import CurrentSource
from tidal_current_grib_generator.sources.pytmd import PyTMDSource
from tidal_current_grib_generator.sources.synthetic import ConstantCurrentSource, SyntheticRotaryTideSource


def create_source(name: str, units: str = "mps", model_directory: Path | None = None) -> CurrentSource:
    normalized = name.strip().lower()
    if normalized == "synthetic":
        return SyntheticRotaryTideSource()
    if normalized == "constant":
        return ConstantCurrentSource(u=1.0, v=0.0, units=units)
    if normalized in {"pytmd", "tpxo"}:
        if model_directory is None:
            raise ValidationError("--model-directory is required for the pyTMD/TPXO source")
        return PyTMDSource(model_directory=model_directory)
    raise UnsupportedSourceError(f"unsupported source {name!r}; choose synthetic, constant, or pytmd")


__all__ = [
    "ConstantCurrentSource",
    "CurrentSource",
    "PyTMDSource",
    "SyntheticRotaryTideSource",
    "create_source",
]
