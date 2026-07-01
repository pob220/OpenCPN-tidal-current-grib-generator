"""Current source registry."""

from __future__ import annotations

from pathlib import Path

from tidal_current_grib_generator.errors import UnsupportedSourceError, ValidationError
from tidal_current_grib_generator.sources.base import CurrentSource
from tidal_current_grib_generator.sources.pytmd import PyTMDSource, PyTMDTPXOSource
from tidal_current_grib_generator.sources.synthetic import ConstantCurrentSource, SyntheticRotaryTideSource


def create_source(
    name: str,
    units: str = "mps",
    model_directory: Path | None = None,
    model_name: str = "TPXO10-atlas-v2-nc",
    definition_file: Path | None = None,
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
    raise UnsupportedSourceError(f"unsupported source {name!r}; choose synthetic, constant, or tpxo")


__all__ = [
    "ConstantCurrentSource",
    "CurrentSource",
    "PyTMDSource",
    "PyTMDTPXOSource",
    "SyntheticRotaryTideSource",
    "create_source",
]
