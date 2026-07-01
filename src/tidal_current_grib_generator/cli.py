"""Command-line interface."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tidal_current_grib_generator.errors import TidalCurrentGribError, ValidationError
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, build_time_sequence, parse_utc_datetime
from tidal_current_grib_generator.grib.validation import inspect_grib, scan_grib_messages
from tidal_current_grib_generator.grib.read import sample_current_components
from tidal_current_grib_generator.grib.writer import EccodesGrib1CurrentWriter
from tidal_current_grib_generator.model import components_to_speed_direction
from tidal_current_grib_generator.reference import compare_reference_csv
from tidal_current_grib_generator.sources import create_source
from tidal_current_grib_generator.sources.netcdf import NetCDFCurrentSource, inspect_netcdf
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
    _add_netcdf_options(generate)
    generate.add_argument("--output", type=Path, required=True)
    generate.add_argument("--format", choices=["grib1"], default="grib1")
    generate.add_argument("--units", choices=["knots", "mps"], default="mps")
    generate.add_argument("--dry-run", action="store_true")
    generate.add_argument("--metadata-summary", action="store_true")
    generate.add_argument("--json-summary", action="store_true")
    generate.add_argument("--clip-bbox-to-source", action="store_true")
    generate.add_argument("--use-source-grid", action="store_true")
    generate.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate.set_defaults(func=cmd_generate)

    compare = subparsers.add_parser("compare-reference", help="Compare source predictions to a CSV.")
    compare.add_argument("--source", required=True)
    compare.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path)
    compare.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    compare.add_argument("--definition-file", type=Path)
    _add_netcdf_options(compare)
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
    _add_netcdf_options(sample)
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

    inspect_nc = subparsers.add_parser("inspect-netcdf", help="Inspect a local NetCDF current file.")
    inspect_nc.add_argument("--input-netcdf", type=Path, required=True)
    inspect_nc.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    inspect_nc.set_defaults(func=cmd_inspect_netcdf)

    inspect_grib_parser = subparsers.add_parser("inspect-grib", help="Inspect a GRIB file.")
    inspect_grib_parser.add_argument("file", type=Path)
    inspect_grib_parser.add_argument("--json", action="store_true", help="Print JSON output.")
    inspect_grib_parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    inspect_grib_parser.set_defaults(func=cmd_inspect_grib)

    validate = subparsers.add_parser("validate-generated", help="Compare generated GRIB values with source NetCDF.")
    validate.add_argument("--input-netcdf", type=Path, required=True)
    validate.add_argument("--generated-grib", type=Path, required=True)
    validate.add_argument("--points", type=Path, required=True)
    validate.add_argument("--output", type=Path)
    _add_netcdf_options(validate, include_input=False)
    validate.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    validate.set_defaults(func=cmd_validate_generated)
    return parser


def _add_netcdf_options(parser: argparse.ArgumentParser, include_input: bool = True) -> None:
    if include_input:
        parser.add_argument("--input-netcdf", type=Path)
    parser.add_argument("--u-variable")
    parser.add_argument("--v-variable")
    parser.add_argument("--lat-variable")
    parser.add_argument("--lon-variable")
    parser.add_argument("--time-variable")
    parser.add_argument("--depth-index", type=int)
    parser.add_argument("--depth-value", type=float)
    parser.add_argument("--assume-units", choices=["mps", "cmps"])
    parser.add_argument("--nearest-time", action="store_true")
    parser.add_argument("--coverage-tolerance-deg", type=float, default=0.02)


def cmd_generate(args: argparse.Namespace) -> int:
    requested_bbox = BoundingBox.from_values(args.bbox)
    start = parse_utc_datetime(args.start)
    times = build_time_sequence(start, args.hours, args.step_hours)
    source = _source_from_args(args)
    plan = _build_generation_plan(args, source, requested_bbox, times)
    output = args.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")

    if args.json_summary and args.dry_run:
        print(json.dumps(_summary_json(plan, source, output, args.dry_run), indent=2, sort_keys=True))
    elif args.metadata_summary or args.dry_run:
        print(
            "\n".join(
                [
                    f"source: {source.describe().name}",
                    f"bbox: {plan.bbox.west},{plan.bbox.south},{plan.bbox.east},{plan.bbox.north}",
                    f"grid: {plan.grid.nx} x {plan.grid.ny} ({plan.grid.nx * plan.grid.ny} points)",
                    f"times: {times[0].isoformat()} to {times[-1].isoformat()} ({len(times)} steps)",
                    f"format: {args.format}",
                    f"messages: {plan.message_count} (u/v current components)",
                    f"output: {output}",
                ]
            )
        )
    if args.dry_run:
        return 0

    grids = (source.get_current_grid(plan.bbox, time, plan.grid) for time in times)
    writer = EccodesGrib1CurrentWriter()
    summary = writer.write(grids, output, progress_callback=_progress_callback(args.verbose))
    scan = scan_grib_messages(summary.output)
    if args.json_summary:
        result = _summary_json(plan, source, summary.output, dry_run=False)
        result["written_messages"] = summary.message_count
        result["validated_messages"] = scan.message_count
        result["bytes"] = scan.byte_count
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
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
    _print_mapping(inspection)
    return 0


def cmd_inspect_netcdf(args: argparse.Namespace) -> int:
    inspection = inspect_netcdf(args.input_netcdf)
    _print_mapping(inspection)
    return 0


def cmd_inspect_grib(args: argparse.Namespace) -> int:
    inspection = inspect_grib(args.file)
    if args.json:
        print(json.dumps(inspection, indent=2, sort_keys=True))
    else:
        _print_mapping(inspection)
    return 0


def cmd_validate_generated(args: argparse.Namespace) -> int:
    source = create_source(
        "netcdf",
        input_netcdf=args.input_netcdf,
        u_variable=args.u_variable,
        v_variable=args.v_variable,
        lat_variable=args.lat_variable,
        lon_variable=args.lon_variable,
        time_variable=args.time_variable,
        depth_index=args.depth_index,
        depth_value=args.depth_value,
        assume_units=args.assume_units,
        nearest_time=args.nearest_time,
        coverage_tolerance_deg=args.coverage_tolerance_deg,
    )
    rows = []
    with args.points.open(newline="") as handle:
        for raw in csv.DictReader(handle):
            lat = float(raw["lat"])
            lon = float(raw["lon"])
            time = parse_utc_datetime(raw["time_utc"])
            bbox = BoundingBox(lon, lat, lon + 0.01, lat + 0.01)
            grid = build_regular_grid(bbox, 0.01)
            source_grid = source.get_current_grid(bbox, time, grid)
            src_u = float(source_grid.u_mps[0, 0])
            src_v = float(source_grid.v_mps[0, 0])
            grib_u, grib_v = sample_current_components(args.generated_grib, lat, lon, time)
            rows.append(
                {
                    "name": raw.get("name", ""),
                    "lat": lat,
                    "lon": lon,
                    "time_utc": time.isoformat().replace("+00:00", "Z"),
                    "source_u_mps": src_u,
                    "source_v_mps": src_v,
                    "grib_u_mps": grib_u,
                    "grib_v_mps": grib_v,
                    "u_error_mps": grib_u - src_u,
                    "v_error_mps": grib_v - src_v,
                }
            )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} validation rows to {args.output}")
    else:
        print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def _print_mapping(inspection: dict) -> None:
    for key, value in inspection.items():
        if isinstance(value, list):
            print(f"{key}: {', '.join(value) if value else '(none)'}")
        elif isinstance(value, dict):
            print(f"{key}:")
            for nested_key, nested_value in value.items():
                print(f"  {nested_key}: {nested_value}")
        else:
            print(f"{key}: {value}")


def _source_from_args(args: argparse.Namespace):
    return create_source(
        args.source,
        units=getattr(args, "units", "mps"),
        model_directory=getattr(args, "model_directory", None),
        model_name=getattr(args, "model_name", DEFAULT_TPXO_MODEL),
        definition_file=getattr(args, "definition_file", None),
        input_netcdf=getattr(args, "input_netcdf", None),
        u_variable=getattr(args, "u_variable", None),
        v_variable=getattr(args, "v_variable", None),
        lat_variable=getattr(args, "lat_variable", None),
        lon_variable=getattr(args, "lon_variable", None),
        time_variable=getattr(args, "time_variable", None),
        depth_index=getattr(args, "depth_index", None),
        depth_value=getattr(args, "depth_value", None),
        assume_units=getattr(args, "assume_units", None),
        nearest_time=getattr(args, "nearest_time", False),
        coverage_tolerance_deg=getattr(args, "coverage_tolerance_deg", 0.02),
        use_source_grid=getattr(args, "use_source_grid", False),
    )


@dataclass(frozen=True)
class GenerationPlan:
    requested_bbox: BoundingBox
    bbox: BoundingBox
    grid: Any
    times: list
    bbox_clipped: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return len(self.times) * 2


def _build_generation_plan(
    args: argparse.Namespace,
    source: Any,
    requested_bbox: BoundingBox,
    times: list,
) -> GenerationPlan:
    bbox = requested_bbox
    warnings: list[str] = []
    clipped = False
    if isinstance(source, NetCDFCurrentSource) and args.clip_bbox_to_source:
        bbox = source.clip_bbox_to_source(requested_bbox)
        clipped = bbox != requested_bbox
        if clipped:
            warnings.append("requested bbox was clipped to NetCDF source coordinate range")
    if isinstance(source, NetCDFCurrentSource) and args.use_source_grid:
        grid = source.build_source_grid(bbox)
        if args.grid_spacing_deg is not None:
            warnings.append("--use-source-grid uses native NetCDF coordinate centres; --grid-spacing-deg is ignored")
    else:
        grid = build_regular_grid(bbox, args.grid_spacing_deg)
    return GenerationPlan(
        requested_bbox=requested_bbox,
        bbox=bbox,
        grid=grid,
        times=times,
        bbox_clipped=clipped,
        warnings=warnings,
    )


def _summary_json(plan: GenerationPlan, source: Any, output: Path, dry_run: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if hasattr(source, "metadata"):
        try:
            metadata = source.metadata(plan.bbox, plan.grid)
        except TidalCurrentGribError as exc:
            metadata = {"metadata_error": str(exc)}
    summary = {
        "source": source.describe().name,
        "input_file": metadata.get("input_file") or _source_input_file(source),
        "output_file": str(output),
        "dry_run": dry_run,
        "requested_bbox": _bbox_dict(plan.requested_bbox),
        "bbox": _bbox_dict(plan.bbox),
        "actual_output_grid_bounds": {
            "west": float(plan.grid.longitudes[0]),
            "south": float(plan.grid.latitudes[0]),
            "east": float(plan.grid.longitudes[-1]),
            "north": float(plan.grid.latitudes[-1]),
        },
        "actual_source_bounds": metadata.get("source_bounds"),
        "grid_size": {"nx": plan.grid.nx, "ny": plan.grid.ny, "points": plan.grid.nx * plan.grid.ny},
        "time_range": {
            "start": plan.times[0].isoformat(),
            "end": plan.times[-1].isoformat(),
            "step_count": len(plan.times),
        },
        "message_count": plan.message_count,
        "u_variable": metadata.get("u_variable"),
        "v_variable": metadata.get("v_variable"),
        "units": metadata.get("units"),
        "interpolation_used": metadata.get("interpolation_used"),
        "bbox_clipped": plan.bbox_clipped,
        "warnings": plan.warnings,
    }
    summary.update({k: v for k, v in metadata.items() if k not in summary and k not in {"input_file"}})
    return summary


def _bbox_dict(bbox: BoundingBox) -> dict[str, float]:
    return {"west": bbox.west, "south": bbox.south, "east": bbox.east, "north": bbox.north}


def _source_input_file(source: Any) -> str | None:
    path = getattr(source, "input_netcdf", None)
    return str(path) if path is not None else None


def _progress_callback(verbose: bool):
    if not verbose:
        return None

    def callback(message_count: int, current) -> None:
        LOGGER.info("wrote %s messages through valid time %s", message_count, current.time.isoformat())

    return callback


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
