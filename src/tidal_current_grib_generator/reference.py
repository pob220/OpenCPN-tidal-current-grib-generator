"""Reference-point comparison support."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, parse_utc_datetime
from tidal_current_grib_generator.model import components_to_speed_direction, direction_error_degrees
from tidal_current_grib_generator.sources.base import CurrentSource


@dataclass(frozen=True)
class ComparisonRow:
    name: str
    lat: float
    lon: float
    time_utc: str
    predicted_speed_knots: float
    predicted_direction_degrees: float
    reference_speed_knots: float
    reference_direction_degrees: float
    speed_error_knots: float
    direction_error_degrees: float
    source_note: str


def compare_reference_csv(source: CurrentSource, reference_csv: Path, output_csv: Path) -> list[ComparisonRow]:
    rows: list[ComparisonRow] = []
    with reference_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            lat = float(raw["lat"])
            lon = float(raw["lon"])
            time = parse_utc_datetime(raw["time_utc"])
            grid = build_regular_grid(BoundingBox(lon, lat, lon + 0.01, lat + 0.01), 0.01)
            current = source.get_current_grid(BoundingBox(lon, lat, lon + 0.01, lat + 0.01), time, grid)
            speed, direction = components_to_speed_direction(
                float(current.u_mps[0, 0]),
                float(current.v_mps[0, 0]),
            )
            ref_speed = float(raw["reference_speed_knots"])
            ref_direction = float(raw["reference_direction_degrees"])
            rows.append(
                ComparisonRow(
                    name=raw["name"],
                    lat=lat,
                    lon=lon,
                    time_utc=time.isoformat().replace("+00:00", "Z"),
                    predicted_speed_knots=speed,
                    predicted_direction_degrees=direction,
                    reference_speed_knots=ref_speed,
                    reference_direction_degrees=ref_direction,
                    speed_error_knots=speed - ref_speed,
                    direction_error_degrees=direction_error_degrees(direction, ref_direction),
                    source_note=raw.get("source_note", ""),
                )
            )

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="") as handle:
        fieldnames = [field.name for field in ComparisonRow.__dataclass_fields__.values()]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)
    return rows
