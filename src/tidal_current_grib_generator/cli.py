"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from tidal_current_grib_generator.errors import TidalCurrentGribError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, build_time_sequence, parse_utc_datetime
from tidal_current_grib_generator.grib.validation import scan_grib_messages
from tidal_current_grib_generator.grib.writer import EccodesGrib1CurrentWriter
from tidal_current_grib_generator.model import components_to_speed_direction
from tidal_current_grib_generator.reference import compare_reference_csv
from tidal_current_grib_generator.sources import create_source
from tidal_current_grib_generator.sources.pytmd import inspect_pytmd_source

LOGGER = logging.getLogger("tidal_current_grib_generator")
DEFAULT_TPXO_MODEL = "TPXO10-atlas-v2-nc"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tidal-current-grib")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate a current GRIB.")
    generate.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    generate.add_argument("--start", required=True, help="UTC ISO-8601 start time, e.g. 2026-07-01T00:00:00Z.")
    generate.add_argument("--hours", type=int, required=True)
    generate.add_argument("--step-hours", type=int, default=1)
    generate.add_argument("--grid-spacing-deg", type=float, required=True)
    generate.add_argument("--source", default="synthetic")
    generate.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path)
    generate.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    generate.add_argument("--definition-file", type=Path)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--format", choices=["grib1"], default="grib1")
    generate.add_argument("--units", choices=["knots", "mps"], default="mps")
    generate.add_argument("--dry-run", action="store_true")
    generate.add_argument("--metadata-summary", action="store_true")
    generate.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate.set_defaults(func=cmd_generate)

    compare = subparsers.add_parser("compare-reference", help="Compare source predictions to a CSV.")
    compare.add_argument("--source", required=True)
    compare.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path)
    compare.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    compare.add_argument("--definition-file", type=Path)
    compare.add_argument("--reference-csv", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--units", choices=["knots", "mps"], default="mps")
    compare.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    compare.set_defaults(func=cmd_compare_reference)

    sample = subparsers.add_parser("sample-point", help="Sample current at one point and time.")
    sample.add_argument("--source", required=True)
    sample.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path)
    sample.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    sample.add_argument("--definition-file", type=Path)
    sample.add_argument("--lat", type=float, required=True)
    sample.add_argument("--lon", type=float, required=True)
    sample.add_argument("--time", required=True)
    sample.add_argument("--units", choices=["knots", "mps"], default="mps")
    sample.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    sample.set_defaults(func=cmd_sample_point)

    inspect = subparsers.add_parser("inspect-source", help="Inspect source/model availability.")
    inspect.add_argument("--source", required=True)
    inspect.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path)
    inspect.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    inspect.add_argument("--definition-file", type=Path)
    inspect.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    inspect.set_defaults(func=cmd_inspect_source)
    return parser


def cmd_generate(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    start = parse_utc_datetime(args.start)
    grid = build_regular_grid(bbox, args.grid_spacing_deg)
    times = build_time_sequence(start, args.hours, args.step_hours)
    source = _source_from_args(args)
    output = args.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")

    message_count = len(times) * 2
    if args.metadata_summary or args.dry_run:
        print(
            "\n".join(
                [
                    f"source: {source.describe().name}",
                    f"bbox: {bbox.west},{bbox.south},{bbox.east},{bbox.north}",
                    f"grid: {grid.nx} x {grid.ny} ({grid.nx * grid.ny} points)",
                    f"times: {times[0].isoformat()} to {times[-1].isoformat()} ({len(times)} steps)",
                    f"format: {args.format}",
                    f"messages: {message_count} (u/v current components)",
                    f"output: {output}",
                ]
            )
        )
    if args.dry_run:
        return 0

    grids = [source.get_current_grid(bbox, time, grid) for time in times]
    writer = EccodesGrib1CurrentWriter()
    summary = writer.write(grids, output)
    scan = scan_grib_messages(summary.output)
    print(f"wrote {summary.message_count} GRIB messages to {summary.output}")
    print(f"validated GRIB stream: {scan.message_count} messages, {scan.byte_count} bytes")
    return 0


def cmd_compare_reference(args: argparse.Namespace) -> int:
    source = _source_from_args(args)
    rows = compare_reference_csv(source, args.reference_csv, args.output)
    print(f"wrote {len(rows)} comparison rows to {args.output}")
    return 0


def cmd_sample_point(args: argparse.Namespace) -> int:
    source = _source_from_args(args)
    lat = float(args.lat)
    lon = float(args.lon)
    if not -90.0 <= lat <= 90.0:
        raise ValidationError("--lat must be within [-90, 90]")
    if not -180.0 <= lon <= 180.0:
        raise ValidationError("--lon must be within [-180, 180]")
    time = parse_utc_datetime(args.time)
    bbox = BoundingBox(lon, lat, lon + 0.01, lat + 0.01)
    grid = build_regular_grid(bbox, 0.01)
    current = source.get_current_grid(bbox, time, grid)
    u = float(current.u_mps[0, 0])
    v = float(current.v_mps[0, 0])
    speed_knots, direction = components_to_speed_direction(u, v)
    description = source.describe()
    print(f"source: {description.name}")
    print(f"summary: {description.summary}")
    print(f"time: {time.isoformat().replace('+00:00', 'Z')}")
    print(f"lat: {lat}")
    print(f"lon: {lon}")
    print(f"u_mps: {u:.6f}")
    print(f"v_mps: {v:.6f}")
    print(f"speed_knots: {speed_knots:.6f}")
    print(f"direction_degrees_true_toward: {direction:.2f}")
    print(f"data_notice: {description.data_notice}")
    return 0


def cmd_inspect_source(args: argparse.Namespace) -> int:
    if args.source.strip().lower() in {"tpxo", "pytmd"}:
        inspection = inspect_pytmd_source(
            model_directory=args.model_directory,
            model_name=args.model_name,
            definition_file=args.definition_file,
        ).as_dict()
    else:
        inspection = create_source(args.source, units="mps").inspect()
    for key, value in inspection.items():
        if isinstance(value, list):
            print(f"{key}: {', '.join(value) if value else '(none)'}")
        else:
            print(f"{key}: {value}")
    return 0


def _source_from_args(args: argparse.Namespace):
    return create_source(
        args.source,
        units=getattr(args, "units", "mps"),
        model_directory=getattr(args, "model_directory", None),
        model_name=getattr(args, "model_name", DEFAULT_TPXO_MODEL),
        definition_file=getattr(args, "definition_file", None),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    try:
        return int(args.func(args))
    except TidalCurrentGribError as exc:
        LOGGER.debug("command failed", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
