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
from tidal_current_grib_generator.reference import compare_reference_csv
from tidal_current_grib_generator.sources import create_source

LOGGER = logging.getLogger("tidal_current_grib_generator")


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
    generate.add_argument("--model-directory", type=Path)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--format", choices=["grib1"], default="grib1")
    generate.add_argument("--units", choices=["knots", "mps"], default="mps")
    generate.add_argument("--dry-run", action="store_true")
    generate.add_argument("--metadata-summary", action="store_true")
    generate.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate.set_defaults(func=cmd_generate)

    compare = subparsers.add_parser("compare-reference", help="Compare source predictions to a CSV.")
    compare.add_argument("--source", required=True)
    compare.add_argument("--model-directory", type=Path)
    compare.add_argument("--reference-csv", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)
    compare.add_argument("--units", choices=["knots", "mps"], default="mps")
    compare.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    compare.set_defaults(func=cmd_compare_reference)
    return parser


def cmd_generate(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    start = parse_utc_datetime(args.start)
    grid = build_regular_grid(bbox, args.grid_spacing_deg)
    times = build_time_sequence(start, args.hours, args.step_hours)
    source = create_source(args.source, units=args.units, model_directory=args.model_directory)
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
    source = create_source(args.source, units=args.units, model_directory=args.model_directory)
    rows = compare_reference_csv(source, args.reference_csv, args.output)
    print(f"wrote {len(rows)} comparison rows to {args.output}")
    return 0


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
