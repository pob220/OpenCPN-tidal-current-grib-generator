from pathlib import Path

import pytest

from tidal_current_grib_generator.errors import ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, parse_utc_datetime
from tidal_current_grib_generator.grib.validation import scan_grib_messages
from tidal_current_grib_generator.grib.writer import EccodesGrib1CurrentWriter
from tidal_current_grib_generator.sources.synthetic import ConstantCurrentSource


def test_grib_message_scan_validates_basic_grib1_message(tmp_path: Path):
    payload = b"GRIB" + (12).to_bytes(3, "big") + b"\x01" + b"7777"
    path = tmp_path / "minimal.grb"
    path.write_bytes(payload)
    result = scan_grib_messages(path)
    assert result.message_count == 1
    assert result.byte_count == 12


def test_grib_message_scan_rejects_bad_terminator(tmp_path: Path):
    path = tmp_path / "bad.grb"
    path.write_bytes(b"GRIB" + (12).to_bytes(3, "big") + b"\x01" + b"xxxx")
    with pytest.raises(ValidationError):
        scan_grib_messages(path)


def test_eccodes_writer_round_trip_if_available(tmp_path: Path):
    pytest.importorskip("eccodes")
    bbox = BoundingBox(-1.0, 50.0, 0.0, 51.0)
    grid = build_regular_grid(bbox, 0.5)
    current = ConstantCurrentSource(u=1.0, v=0.0).get_current_grid(
        bbox, parse_utc_datetime("2026-07-01T00:00:00Z"), grid
    )
    path = tmp_path / "current.grb"
    summary = EccodesGrib1CurrentWriter().write([current], path)
    assert summary.message_count == 2
    assert scan_grib_messages(path).message_count == 2
