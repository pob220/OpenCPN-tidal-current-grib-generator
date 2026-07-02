"""Command-line interface."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import getpass
import os
import tempfile
import time as monotonic_time
import sys
import shutil
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from tidal_current_grib_generator.errors import TidalCurrentGribError, ValidationError
from tidal_current_grib_generator.copernicus import CopernicusDownloadRequest, download_copernicus_subset
from tidal_current_grib_generator.dependencies import check_dependencies
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, build_time_sequence, parse_utc_datetime
from tidal_current_grib_generator.grib.validation import inspect_grib, scan_grib_messages
from tidal_current_grib_generator.grib.read import sample_current_components
from tidal_current_grib_generator.grib.writer import EccodesGrib1CurrentWriter
from tidal_current_grib_generator.model import components_to_speed_direction
from tidal_current_grib_generator.marine_ie import download_marine_ie_irish_sea_grib
from tidal_current_grib_generator.reference import compare_reference_csv
from tidal_current_grib_generator.sources import create_source
from tidal_current_grib_generator.sources.netcdf import NetCDFCurrentSource, inspect_netcdf, netcdf_time_metadata
from tidal_current_grib_generator.sources.pytmd import inspect_pytmd_source
from tidal_current_grib_generator.sources.tpxo_cache import prepare_tpxo_cache, validate_tpxo_cache
from tidal_current_grib_generator.providers import (
    Provider,
    ProviderRegistry,
    select_best_provider_for_bbox,
    select_copernicus_provider,
)
from tidal_current_grib_generator.api import GenerateCurrentGribRequest, generate_current_grib_from_netcdf
from tidal_current_grib_generator.security import redact_text
from tidal_current_grib_generator.weather import (
    GFSWeatherRequest,
    generate_gfs_weather_grib,
    list_weather_providers,
)

LOGGER = logging.getLogger("tidal_current_grib_generator")
DEFAULT_TPXO_MODEL = "TPXO10-atlas-v2-nc"


class RedactingLogFilter(logging.Filter):
    def __init__(self, sensitive_values: list[str]):
        super().__init__()
        self.sensitive_values = sensitive_values

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), self.sensitive_values)
        record.args = ()
        return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tidal-current-grib")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging.")
    parser.add_argument("--debug", action="store_true", help="Enable diagnostic debug logging, including third-party libraries.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate a current GRIB.")
    generate.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    generate.add_argument("--start", required=True, help="UTC ISO-8601 start time, e.g. 2026-07-01T00:00:00Z.")
    generate.add_argument("--hours", type=int, required=True)
    generate.add_argument("--step-hours", type=int, default=1)
    generate.add_argument("--grid-spacing-deg", type=float)
    generate.add_argument("--source", default="synthetic")
    generate.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path)
    generate.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    generate.add_argument("--definition-file", type=Path)
    generate.add_argument("--input-cache", type=Path, help="Local TPXO cache file for --source tpxo-cache.")
    generate.add_argument(
        "--tpxo-workers",
        type=int,
        default=1,
        help="TPXO-only worker count. Values above 1 are currently disabled because benchmarking did not show a safe improvement.",
    )
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
    generate.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
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
    inspect.add_argument("--input-cache", type=Path)
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

    deps = subparsers.add_parser("check-dependencies", help="Check runtime dependencies.")
    deps.add_argument("--output-directory", type=Path)
    deps.add_argument("--json", action="store_true")
    deps.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    deps.set_defaults(func=cmd_check_dependencies)

    providers = subparsers.add_parser("providers", help="List or auto-select providers.")
    providers.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    providers.add_argument("--hours", type=int, help="Requested duration for auto provider selection.")
    providers.add_argument("--json", action="store_true")
    providers.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    providers.set_defaults(func=cmd_providers)

    weather_providers = subparsers.add_parser("weather-providers", help="List weather GRIB providers.")
    weather_providers.add_argument("--json", action="store_true")
    weather_providers.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    weather_providers.set_defaults(func=cmd_weather_providers)

    generate_weather = subparsers.add_parser("generate-weather", help="Download/generate a weather GRIB.")
    generate_weather.add_argument("--provider", choices=["gfs"], required=True)
    generate_weather.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    generate_weather.add_argument("--date", help="GFS cycle date YYYYMMDD for explicit cycles.")
    generate_weather.add_argument("--cycle", required=True, help="auto or explicit cycle 00, 06, 12, 18.")
    generate_weather.add_argument("--hours", type=int, required=True)
    generate_weather.add_argument("--step-hours", type=int, default=3)
    generate_weather.add_argument("--output", type=Path, required=True)
    generate_weather.add_argument("--overwrite", action="store_true")
    generate_weather.add_argument("--dry-run", action="store_true")
    generate_weather.add_argument("--metadata-summary", action="store_true")
    generate_weather.add_argument("--json", action="store_true")
    generate_weather.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate_weather.set_defaults(func=cmd_generate_weather)

    download = subparsers.add_parser("download-copernicus", help="Download a Copernicus Marine current subset.")
    download.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    download.add_argument(
        "--provider",
        choices=["auto", "copernicus_nws", "copernicus_global"],
        default="auto",
        help="Copernicus provider to use; auto prefers NWS inside its coverage and Global elsewhere.",
    )
    download.add_argument("--start", required=True)
    end_group = download.add_mutually_exclusive_group(required=True)
    end_group.add_argument("--end")
    end_group.add_argument("--hours", type=int)
    download.add_argument("--output-directory", type=Path, required=True)
    download.add_argument("--output-filename", required=True)
    download.add_argument("--username")
    download.add_argument("--password-env", default="CURRENTGRIB_TEST_COPERNICUS_PASSWORD")
    download.add_argument("--username-env", default="CURRENTGRIB_TEST_COPERNICUS_USERNAME")
    download.add_argument("--dry-run", action="store_true")
    download.add_argument("--overwrite", action="store_true")
    download.add_argument("--json", action="store_true")
    download.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    download.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    download.set_defaults(func=cmd_download_copernicus)

    generate_copernicus = subparsers.add_parser(
        "generate-copernicus",
        help="Download a Copernicus Marine current subset and convert it to GRIB.",
    )
    generate_copernicus.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    generate_copernicus.add_argument(
        "--provider",
        choices=["auto", "copernicus_nws", "copernicus_global"],
        default="auto",
        help="Copernicus provider to use; auto prefers NWS inside its coverage and Global elsewhere.",
    )
    generate_copernicus.add_argument("--start", required=True)
    generate_copernicus_end = generate_copernicus.add_mutually_exclusive_group(required=True)
    generate_copernicus_end.add_argument("--end")
    generate_copernicus_end.add_argument("--hours", type=int)
    generate_copernicus.add_argument("--step-hours", type=int)
    generate_copernicus.add_argument("--grid-spacing-deg", type=float, default=0.03)
    generate_copernicus.add_argument(
        "--source-grid-regularity-tolerance",
        type=float,
        help="Tolerance for nearly regular native NetCDF coordinate spacing; defaults from provider metadata.",
    )
    generate_copernicus.add_argument("--download-directory", type=Path, required=True)
    generate_copernicus.add_argument("--download-filename")
    generate_copernicus.add_argument("--output", type=Path, required=True)
    generate_copernicus.add_argument("--username")
    generate_copernicus.add_argument("--password-env", default="CURRENTGRIB_COPERNICUS_PASSWORD")
    generate_copernicus.add_argument("--username-env", default="CURRENTGRIB_TEST_COPERNICUS_USERNAME")
    generate_copernicus.add_argument("--overwrite", action="store_true")
    generate_copernicus.add_argument("--dry-run", action="store_true")
    generate_copernicus.add_argument("--json", action="store_true")
    generate_copernicus.add_argument("--metadata-summary", action="store_true")
    generate_copernicus.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate_copernicus.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate_copernicus.set_defaults(func=cmd_generate_copernicus)

    generate_provider = subparsers.add_parser(
        "generate-provider",
        help="Generate/download using a named provider, including direct-current GRIB providers.",
    )
    generate_provider.add_argument(
        "--provider",
        choices=["auto", "marine_ie_irish_sea", "copernicus_nws", "copernicus_global"],
        required=True,
    )
    generate_provider.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"))
    generate_provider.add_argument("--start")
    generate_provider_end = generate_provider.add_mutually_exclusive_group()
    generate_provider_end.add_argument("--end")
    generate_provider_end.add_argument("--hours", type=int)
    generate_provider.add_argument("--step-hours", type=int)
    generate_provider.add_argument("--grid-spacing-deg", type=float, default=0.03)
    generate_provider.add_argument("--source-grid-regularity-tolerance", type=float)
    generate_provider.add_argument("--download-directory", type=Path)
    generate_provider.add_argument("--download-filename")
    generate_provider.add_argument("--output", type=Path, required=True)
    generate_provider.add_argument("--username")
    generate_provider.add_argument("--password-env", default="CURRENTGRIB_COPERNICUS_PASSWORD")
    generate_provider.add_argument("--username-env", default="CURRENTGRIB_TEST_COPERNICUS_USERNAME")
    generate_provider.add_argument("--overwrite", action="store_true")
    generate_provider.add_argument("--dry-run", action="store_true")
    generate_provider.add_argument("--json", action="store_true")
    generate_provider.add_argument("--metadata-summary", action="store_true")
    generate_provider.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate_provider.add_argument("--debug", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate_provider.set_defaults(func=cmd_generate_provider)

    prepare_cache = subparsers.add_parser("prepare-tpxo-cache", help="Prepare a local derived TPXO harmonic-current cache.")
    prepare_cache.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    prepare_cache.add_argument("--grid-spacing-deg", type=float, required=True)
    prepare_cache.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path, required=True)
    prepare_cache.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    prepare_cache.add_argument("--definition-file", type=Path)
    prepare_cache.add_argument("--output", type=Path, required=True)
    prepare_cache.add_argument("--metadata-summary", action="store_true")
    prepare_cache.add_argument("--json", action="store_true")
    prepare_cache.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    prepare_cache.set_defaults(func=cmd_prepare_tpxo_cache)

    benchmark_tpxo = subparsers.add_parser("benchmark-tpxo", help="Benchmark TPXO generation worker counts.")
    benchmark_tpxo.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    benchmark_tpxo.add_argument("--start", required=True)
    benchmark_tpxo.add_argument("--hours", type=int, required=True)
    benchmark_tpxo.add_argument("--step-hours", type=int, default=1)
    benchmark_tpxo.add_argument("--grid-spacing-deg", type=float, required=True)
    benchmark_tpxo.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path, required=True)
    benchmark_tpxo.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    benchmark_tpxo.add_argument("--definition-file", type=Path)
    benchmark_tpxo.add_argument("--workers", required=True, help="Comma-separated worker counts, e.g. 1,2,4.")
    benchmark_tpxo.add_argument("--output-directory", type=Path, help="Directory for temporary benchmark GRIBs.")
    benchmark_tpxo.add_argument("--keep-outputs", action="store_true")
    benchmark_tpxo.add_argument("--json", action="store_true")
    benchmark_tpxo.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    benchmark_tpxo.set_defaults(func=cmd_benchmark_tpxo)
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
    parser.add_argument("--source-grid-regularity-tolerance", type=float, default=1e-5)


def cmd_generate(args: argparse.Namespace) -> int:
    command_started = monotonic_time.perf_counter()
    source_name = args.source.strip().lower()
    if args.tpxo_workers < 1:
        raise ValidationError("--tpxo-workers must be 1 or greater")
    if args.tpxo_workers != 1 and source_name not in {"tpxo", "pytmd"}:
        raise ValidationError("--tpxo-workers is only supported with --source tpxo")
    if args.tpxo_workers != 1:
        raise ValidationError("parallel TPXO workers are disabled; benchmarking did not show a safe runtime improvement")
    start = parse_utc_datetime(args.start)
    times = build_time_sequence(start, args.hours, args.step_hours)
    source = _source_from_args(args)
    if source.describe().name == "tpxo-cache":
        requested_bbox = source.bbox
        plan = GenerationPlan(
            requested_bbox=source.bbox,
            bbox=source.bbox,
            grid=source.grid,
            times=times,
            warnings=[],
        )
    else:
        if args.bbox is None:
            raise ValidationError("--bbox is required unless --source tpxo-cache is used")
        if args.grid_spacing_deg is None:
            raise ValidationError("--grid-spacing-deg is required unless --source tpxo-cache is used")
        requested_bbox = BoundingBox.from_values(args.bbox)
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
                    f"Source: {_source_provenance_label(source)}",
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

    source_name = source.describe().name
    tpxo_timing: dict[str, Any] = {}
    if source_name in {"tpxo", "tpxo-cache"}:
        grids = list(_current_grids_for_generation(source, plan.bbox, times, plan.grid, args.verbose))
        timing_method = getattr(source, "last_timing", None)
        if callable(timing_method):
            tpxo_timing = timing_method()
    else:
        grids = _current_grids_for_generation(source, plan.bbox, times, plan.grid, args.verbose)
    writer = EccodesGrib1CurrentWriter()
    tmp_path: Path | None = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix=output.name + ".", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
        write_started = monotonic_time.perf_counter()
        summary = writer.write(grids, tmp_path, progress_callback=_progress_callback(args.verbose))
        write_seconds = monotonic_time.perf_counter() - write_started
        if summary.message_count != plan.message_count:
            raise ValidationError(
                f"wrote {summary.message_count} messages, expected {plan.message_count}; "
                "discarding incomplete GRIB output"
            )
        validation_started = monotonic_time.perf_counter()
        scan = scan_grib_messages(summary.output)
        validation_seconds = monotonic_time.perf_counter() - validation_started
        if scan.message_count != plan.message_count:
            raise ValidationError(
                f"validated {scan.message_count} messages, expected {plan.message_count}; "
                "discarding incomplete GRIB output"
            )
        os.replace(tmp_path, output)
        tmp_path = None
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
    if args.json_summary:
        result = _summary_json(plan, source, output, dry_run=False)
        result["written_messages"] = summary.message_count
        result["validated_messages"] = scan.message_count
        result["bytes"] = scan.byte_count
        if source_name in {"tpxo", "tpxo-cache"}:
            result["timing"] = _tpxo_generation_timing_dict(
                tpxo_timing,
                write_seconds=write_seconds,
                validation_seconds=validation_seconds,
                total_seconds=monotonic_time.perf_counter() - command_started,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"wrote {summary.message_count} GRIB messages to {output}")
        print(f"validated GRIB stream: {scan.message_count} messages, {scan.byte_count} bytes")
        if args.verbose and source_name in {"tpxo", "tpxo-cache"}:
            _print_tpxo_generation_timing(_tpxo_generation_timing_dict(
                    tpxo_timing,
                    write_seconds=write_seconds,
                    validation_seconds=validation_seconds,
                    total_seconds=monotonic_time.perf_counter() - command_started,
                )
            )
    args._last_generation_timing = _tpxo_generation_timing_dict(
        tpxo_timing,
        write_seconds=write_seconds,
        validation_seconds=validation_seconds,
        total_seconds=monotonic_time.perf_counter() - command_started,
    ) if source_name in {"tpxo", "tpxo-cache"} else {}
    return 0


def cmd_benchmark_tpxo(args: argparse.Namespace) -> int:
    worker_counts = _parse_worker_counts(args.workers)
    benchmark_dir = (
        args.output_directory.expanduser()
        if args.output_directory is not None
        else Path(tempfile.mkdtemp(prefix="tidal-current-grib-tpxo-benchmark-"))
    )
    benchmark_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    print(
        "TPXO benchmark warning: multiple workers can duplicate TPXO NetCDF model opening, memory use, and disk I/O. "
        "Do not assume more workers is faster.",
        flush=True,
    )
    for workers in worker_counts:
        output = benchmark_dir / f"benchmark_tpxo_workers_{workers}.grb"
        if workers != 1:
            result = {
                "workers": workers,
                "skipped": True,
                "reason": "parallel TPXO workers are disabled; a real 24h benchmark exceeded single-worker runtime and did not complete promptly",
            }
            results.append(result)
            print(f"benchmark workers={workers}: skipped ({result['reason']})", flush=True)
            continue
        if output.exists():
            output.unlink()
        run_args = argparse.Namespace(
            bbox=args.bbox,
            start=args.start,
            hours=args.hours,
            step_hours=args.step_hours,
            grid_spacing_deg=args.grid_spacing_deg,
            source="tpxo",
            model_directory=args.model_directory,
            model_name=args.model_name,
            definition_file=args.definition_file,
            tpxo_workers=workers,
            input_netcdf=None,
            u_variable=None,
            v_variable=None,
            lat_variable=None,
            lon_variable=None,
            time_variable=None,
            depth_index=None,
            depth_value=None,
            assume_units=None,
            nearest_time=False,
            coverage_tolerance_deg=0.02,
            source_grid_regularity_tolerance=1e-5,
            use_source_grid=False,
            output=output,
            format="grib1",
            units="mps",
            dry_run=False,
            metadata_summary=True,
            json_summary=False,
            verbose=True,
        )
        print(f"benchmark workers={workers}", flush=True)
        started = monotonic_time.perf_counter()
        rc = cmd_generate(run_args)
        elapsed = monotonic_time.perf_counter() - started
        inspection = inspect_grib(output)
        result = {
            "workers": workers,
            "output": str(output),
            "runtime_seconds": elapsed,
            "timing": getattr(run_args, "_last_generation_timing", {}),
            "message_count": inspection.get("message_count"),
            "stream_valid": inspection.get("stream_valid"),
            "parameter_counts": inspection.get("parameter_counts"),
            "first_valid_time": inspection.get("first_valid_time"),
            "last_valid_time": inspection.get("last_valid_time"),
        }
        results.append(result)
        if not args.keep_outputs:
            output.unlink(missing_ok=True)
    if not args.keep_outputs and args.output_directory is None:
        shutil.rmtree(benchmark_dir, ignore_errors=True)
    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print("TPXO benchmark summary:", flush=True)
        for result in results:
            if result.get("skipped"):
                print(f"  workers={result['workers']}: skipped - {result['reason']}", flush=True)
                continue
            timing = result.get("timing", {})
            print(
                "  workers={workers}: total={total:.2f}s, pyTMD={pytmd}, write={write}, "
                "messages={messages}, valid={valid}".format(
                    workers=result["workers"],
                    total=float(result["runtime_seconds"]),
                    pytmd=_format_seconds(timing.get("pytmd_compute_seconds")),
                    write=_format_seconds(timing.get("grib_write_seconds")),
                    messages=result.get("message_count"),
                    valid=result.get("stream_valid"),
                ),
                flush=True,
            )
    return 0


def cmd_prepare_tpxo_cache(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    output = args.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a cache file path, not a directory")
    if args.metadata_summary:
        print("preparing TPXO cache", flush=True)
        print(f"Source: TPXO10 astronomical tide model", flush=True)
        print(f"bbox: {bbox.west},{bbox.south},{bbox.east},{bbox.north}", flush=True)
        print(f"grid_spacing_deg: {args.grid_spacing_deg}", flush=True)
        print(f"output: {output}", flush=True)
        print("notice: Derived from local licensed TPXO model files. Do not redistribute unless your TPXO licence permits it.", flush=True)
    prepared = prepare_tpxo_cache(
        bbox=bbox,
        grid_spacing_deg=args.grid_spacing_deg,
        model_directory=args.model_directory,
        model_name=args.model_name,
        definition_file=args.definition_file,
        output=output,
        verbose=args.verbose,
    )
    summary = prepared.summary()
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(f"wrote TPXO cache: {prepared.path}", flush=True)
        print(
            f"cache grid: {prepared.grid.nx} x {prepared.grid.ny} "
            f"({prepared.point_count} points)",
            flush=True,
        )
        print(f"constituents: {', '.join(prepared.metadata.constituents)}", flush=True)
        print(f"preparation_seconds: {prepared.preparation_seconds:.2f}", flush=True)
        print("notice: Derived from local licensed TPXO model files. Do not redistribute unless your TPXO licence permits it.", flush=True)
    return 0


def _parse_worker_counts(value: str) -> list[int]:
    counts: list[int] = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            count = int(raw)
        except ValueError as exc:
            raise ValidationError("--workers must be a comma-separated list of positive integers") from exc
        if count < 1:
            raise ValidationError("--workers values must be 1 or greater")
        if count > 8:
            raise ValidationError("--workers values above 8 are not supported; TPXO jobs are I/O and memory heavy")
        counts.append(count)
    if not counts:
        raise ValidationError("--workers must include at least one worker count")
    return counts


def _current_grids_for_generation(source: Any, bbox: BoundingBox, times: list[Any], grid: Any, verbose: bool):
    batch_method = getattr(source, "get_current_grids", None)
    if callable(batch_method):
        started = monotonic_time.perf_counter()
        grids = batch_method(bbox, times, grid)
        elapsed = monotonic_time.perf_counter() - started
        if verbose:
            print(f"computed {len(grids)} current grids in {elapsed:.2f}s", flush=True)
        for index, current in enumerate(grids, start=1):
            if verbose:
                print(f"prepared timestep {index}/{len(times)}: {current.time.isoformat()}", flush=True)
            yield current
        return
    for index, valid_time in enumerate(times, start=1):
        started = monotonic_time.perf_counter()
        current = source.get_current_grid(bbox, valid_time, grid)
        elapsed = monotonic_time.perf_counter() - started
        if verbose:
            print(f"computed timestep {index}/{len(times)} {valid_time.isoformat()} in {elapsed:.2f}s", flush=True)
        yield current


def _tpxo_generation_timing_dict(
    source_timing: dict[str, Any],
    *,
    write_seconds: float,
    validation_seconds: float,
    total_seconds: float,
) -> dict[str, Any]:
    return {
        "workers": source_timing.get("workers", 1),
        "model_open_seconds": source_timing.get("model_open_seconds"),
        "coordinate_grid_size": source_timing.get("coordinate_grid_size"),
        "point_count": source_timing.get("point_count"),
        "timestep_count": source_timing.get("timestep_count"),
        "pytmd_compute_seconds": source_timing.get("pytmd_compute_seconds"),
        "cache_predict_seconds": source_timing.get("cache_predict_seconds"),
        "grib_write_seconds": write_seconds,
        "grib_validation_seconds": validation_seconds,
        "total_generation_seconds": total_seconds,
        "worker_timings": source_timing.get("worker_timings"),
    }


def _print_tpxo_generation_timing(timing: dict[str, Any]) -> None:
    grid_size = timing.get("coordinate_grid_size") or {}
    print("TPXO timing:", flush=True)
    print(f"  workers: {timing.get('workers')}", flush=True)
    print(f"  model_open_seconds: {_format_seconds(timing.get('model_open_seconds'))}", flush=True)
    print(f"  coordinate_grid_size: {grid_size.get('nx')} x {grid_size.get('ny')}", flush=True)
    print(f"  point_count: {timing.get('point_count')}", flush=True)
    print(f"  timestep_count: {timing.get('timestep_count')}", flush=True)
    print(f"  pytmd_compute_seconds: {_format_seconds(timing.get('pytmd_compute_seconds'))}", flush=True)
    if timing.get("cache_predict_seconds") is not None:
        print(f"  cache_predict_seconds: {_format_seconds(timing.get('cache_predict_seconds'))}", flush=True)
    print(f"  grib_write_seconds: {_format_seconds(timing.get('grib_write_seconds'))}", flush=True)
    print(f"  grib_validation_seconds: {_format_seconds(timing.get('grib_validation_seconds'))}", flush=True)
    print(f"  total_generation_seconds: {_format_seconds(timing.get('total_generation_seconds'))}", flush=True)


def _format_seconds(value: Any) -> str:
    if value is None:
        return "(unknown)"
    return f"{float(value):.2f}"


def _source_provenance_label(source: Any) -> str:
    description = source.describe()
    if description.name == "tpxo":
        return "TPXO10 astronomical tide model"
    if description.name == "tpxo-cache":
        return "TPXO10 astronomical tide model cache"
    if description.name == "netcdf":
        return "Local NetCDF model current"
    if description.name == "synthetic":
        return "Synthetic test current"
    return description.summary


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
    elif args.source.strip().lower() in {"tpxo-cache", "tpxo_cache"}:
        if getattr(args, "input_cache", None) is None:
            raise ValidationError("--input-cache is required for inspecting a TPXO cache")
        inspection = validate_tpxo_cache(args.input_cache)
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


def cmd_check_dependencies(args: argparse.Namespace) -> int:
    status = check_dependencies(args.output_directory).as_dict()
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        _print_mapping(status)
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    registry = ProviderRegistry()
    data: dict[str, Any] = {"providers": [provider.as_dict() for provider in registry.list()]}
    if args.bbox:
        bbox = BoundingBox.from_values(args.bbox)
        selected = select_best_provider_for_bbox(bbox, duration_hours=args.hours, registry=registry)
        data["selected"] = selected.as_dict() if selected else None
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        for provider in data["providers"]:
            print(f"{provider['id']}: {provider['label']} ({'implemented' if provider['implemented'] else 'stub'})")
        if "selected" in data:
            print(f"selected: {data['selected']['id'] if data['selected'] else '(none)'}")
    return 0


def cmd_weather_providers(args: argparse.Namespace) -> int:
    providers = [provider.as_dict() for provider in list_weather_providers()]
    if args.json:
        print(json.dumps({"providers": providers}, indent=2, sort_keys=True))
    else:
        for provider in providers:
            print(f"{provider['id']}: {provider['label']}")
            print(f"  {provider['account']}")
            print(f"  {provider['format']}")
            print(f"  source: {provider['source']}")
    return 0


def cmd_generate_weather(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    if args.provider != "gfs":
        raise ValidationError(f"unsupported weather provider: {args.provider}")
    request = GFSWeatherRequest(
        bbox=bbox,
        output=args.output,
        hours=args.hours,
        step_hours=args.step_hours,
        cycle=args.cycle,
        date=args.date,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    if args.metadata_summary or args.dry_run:
        print("Source: NOAA GFS 0.25° forecast via NOMADS", flush=True)
        print(f"provider: {args.provider}", flush=True)
        print(f"bbox: {bbox.west},{bbox.south},{bbox.east},{bbox.north}", flush=True)
        print(f"hours: {args.hours}", flush=True)
        print(f"step_hours: {args.step_hours}", flush=True)
        print(f"output: {args.output.expanduser()}", flush=True)
    result = generate_gfs_weather_grib(
        request,
        progress_callback=_weather_progress_callback(args.verbose or args.metadata_summary),
    )
    data = result.as_dict()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"cycle: {result.cycle.cycle_time}", flush=True)
        print(f"forecast_hours: {','.join(str(hour) for hour in result.forecast_hours)}", flush=True)
        if args.dry_run:
            print(f"planned output: {result.output}", flush=True)
        else:
            print(f"wrote weather GRIB: {result.output}", flush=True)
            print(f"validated GRIB stream: {result.message_count} messages, {result.byte_count} bytes", flush=True)
            first_valid = result.inspection.get("first_valid_time")
            last_valid = result.inspection.get("last_valid_time")
            if first_valid and last_valid:
                print(f"valid_time_range: {first_valid} to {last_valid}", flush=True)
    return 0


def cmd_generate_provider(args: argparse.Namespace) -> int:
    provider_id = args.provider
    if provider_id == "auto":
        if not args.bbox:
            raise ValidationError("--bbox is required when --provider auto is used")
        selected = select_best_provider_for_bbox(
            BoundingBox.from_values(args.bbox),
            duration_hours=args.hours,
            registry=ProviderRegistry(),
        )
        if selected is None:
            raise ValidationError("no implemented provider supports the requested bbox")
        provider_id = selected.id
        if args.metadata_summary:
            print(f"selected provider: {provider_id} ({selected.label})", flush=True)

    if provider_id == "marine_ie_irish_sea":
        if args.dry_run:
            data = {
                "provider": provider_id,
                "output": str(args.output.expanduser()),
                "dry_run": True,
                "operation": "download ready-made current GRIB",
            }
            print(json.dumps(data, indent=2, sort_keys=True) if args.json else f"planned output: {args.output}")
            return 0
        if args.metadata_summary:
            print("checking inputs", flush=True)
            print("selected provider: marine_ie_irish_sea (Marine Institute Ireland Irish Sea currents)", flush=True)
            print("downloading ready-made current GRIB", flush=True)
        result = download_marine_ie_irish_sea_grib(
            args.output,
            overwrite=args.overwrite,
            progress_callback=_direct_grib_progress_callback(args.verbose),
        )
        if args.metadata_summary:
            print("validating GRIB stream", flush=True)
        if args.json:
            print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
        else:
            inspection = result.inspection
            print(f"wrote current GRIB: {result.output}", flush=True)
            if inspection.get("raw_byte_count") is not None:
                print(f"raw downloaded size: {inspection.get('raw_byte_count')} bytes", flush=True)
                print(f"cleaned GRIB size: {inspection.get('clean_byte_count')} bytes", flush=True)
                print(f"skipped non-GRIB bytes: {inspection.get('skipped_byte_count')}", flush=True)
            print(f"validated GRIB stream: {inspection.get('message_count')} messages", flush=True)
            if inspection.get("edition_counts"):
                print(f"edition counts: {inspection.get('edition_counts')}", flush=True)
            if inspection.get("parameter_counts"):
                print(f"parameter counts: {inspection.get('parameter_counts')}", flush=True)
            if inspection.get("current_component_counts"):
                print(f"current components: {inspection.get('current_component_counts')}", flush=True)
            if inspection.get("first_valid_time") or inspection.get("last_valid_time"):
                print(
                    f"valid time range: {inspection.get('first_valid_time')} to {inspection.get('last_valid_time')}",
                    flush=True,
                )
            print("complete", flush=True)
        return 0

    if provider_id in {"copernicus_nws", "copernicus_global"}:
        if not args.bbox:
            raise ValidationError("--bbox is required for Copernicus providers")
        if not args.start:
            raise ValidationError("--start is required for Copernicus providers")
        if args.hours is None and args.end is None:
            raise ValidationError("one of --hours or --end is required for Copernicus providers")
        if args.download_directory is None:
            raise ValidationError("--download-directory is required for Copernicus providers")
        args.provider = provider_id
        return cmd_generate_copernicus(args)

    raise ValidationError(f"unsupported provider for generate-provider: {provider_id}")


def _select_copernicus_provider(provider_id: str, bbox: BoundingBox) -> Provider:
    try:
        return select_copernicus_provider(provider_id, bbox, registry=ProviderRegistry())
    except (KeyError, ValueError) as exc:
        raise ValidationError(str(exc)) from exc


def cmd_download_copernicus(args: argparse.Namespace) -> int:
    start = parse_utc_datetime(args.start)
    if args.hours is not None:
        if args.hours <= 0:
            raise ValidationError("--hours must be greater than zero")
        end = start + timedelta(hours=args.hours)
    else:
        end = parse_utc_datetime(args.end)
    username = args.username or os.environ.get(args.username_env)
    if not username:
        username = input("Copernicus username: ")
    password = os.environ.get(args.password_env)
    if not password:
        password = getpass.getpass("Copernicus password: ")
    bbox = BoundingBox.from_values(args.bbox)
    provider = _select_copernicus_provider(args.provider, bbox)
    request = CopernicusDownloadRequest(
        bbox=bbox,
        start=start,
        end=end,
        output_directory=args.output_directory,
        output_filename=args.output_filename,
        username=username,
        password=password,
        dataset_id=provider.dataset_id or "",
        variables=provider.variables,
        minimum_depth=provider.minimum_depth,
        maximum_depth=provider.maximum_depth,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    result = download_copernicus_subset(
        request,
        progress_callback=_download_progress_callback(args.verbose),
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"downloaded NetCDF: {result.path}")
    return 0


def cmd_generate_copernicus(args: argparse.Namespace) -> int:
    start = parse_utc_datetime(args.start)
    if args.hours is not None:
        if args.hours <= 0:
            raise ValidationError("--hours must be greater than zero")
        end = start + timedelta(hours=args.hours)
        hours = args.hours
    else:
        end = parse_utc_datetime(args.end)
        seconds = int((end - start).total_seconds())
        if seconds <= 0:
            raise ValidationError("--end must be after --start")
        if seconds % 3600:
            raise ValidationError("--end must fall on an exact hour relative to --start")
        hours = seconds // 3600
    bbox = BoundingBox.from_values(args.bbox)
    provider = _select_copernicus_provider(args.provider, bbox)
    step_hours = args.step_hours or provider.default_step_hours
    source_grid_regularity_tolerance = (
        args.source_grid_regularity_tolerance
        if args.source_grid_regularity_tolerance is not None
        else provider.source_grid_regularity_tolerance
    )
    if step_hours <= 0:
        raise ValidationError("--step-hours must be greater than zero")
    if source_grid_regularity_tolerance <= 0:
        raise ValidationError("--source-grid-regularity-tolerance must be greater than zero")

    username = args.username or os.environ.get(args.username_env)
    if not username:
        username = input("Copernicus username: ")
    password = os.environ.get(args.password_env)
    if not password:
        password = getpass.getpass("Copernicus password: ")

    download_filename = args.download_filename or (
        f"{provider.id}_currents_{start:%Y%m%dT%H%MZ}_{hours}h.nc"
    )
    download_request = CopernicusDownloadRequest(
        bbox=bbox,
        start=start,
        end=end,
        output_directory=args.download_directory,
        output_filename=download_filename,
        username=username,
        password=password,
        dataset_id=provider.dataset_id or "",
        variables=provider.variables,
        minimum_depth=provider.minimum_depth,
        maximum_depth=provider.maximum_depth,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    requested_end = end
    requested_hours = hours
    if args.metadata_summary:
        print("checking inputs", flush=True)
        print(
            f"requested time range: start={start.isoformat()} end={requested_end.isoformat()} "
            f"hours={requested_hours} step_hours={step_hours}",
            flush=True,
        )
        print(f"selected provider: {provider.id} ({provider.label})", flush=True)
        print(f"dataset: {provider.dataset_id}", flush=True)
        print(f"source grid regularity tolerance: {source_grid_regularity_tolerance}", flush=True)
        print("downloading Copernicus NetCDF", flush=True)
    download_result = download_copernicus_subset(
        download_request,
        progress_callback=_download_progress_callback(args.verbose),
    )
    if args.dry_run:
        data = {"download": download_result.as_dict(), "dry_run": True}
        print(json.dumps(data, indent=2, sort_keys=True) if args.json else f"planned NetCDF: {download_result.path}")
        return 0

    if args.metadata_summary:
        print(f"downloaded NetCDF path: {download_result.path}", flush=True)
        print("inspecting NetCDF", flush=True)
    time_plan = _generation_time_plan_from_netcdf(download_result.path, start, requested_end, step_hours)
    generation_start = time_plan["start"]
    generation_end = time_plan["end"]
    generation_hours = time_plan["hours"]
    if args.metadata_summary:
        print(
            "source time range: "
            f"first={time_plan['source_first_time'].isoformat()} "
            f"last={time_plan['source_last_time'].isoformat()} "
            f"count={time_plan['source_time_count']} "
            f"step_hours={time_plan['source_step_hours']}",
            flush=True,
        )
        if generation_start != start:
            print(
                "Requested start time adjusted from "
                f"{start.isoformat()} to first available Copernicus time {generation_start.isoformat()}.",
                flush=True,
            )
        print(
            "adjusted generation time range: "
            f"start={generation_start.isoformat()} end={generation_end.isoformat()} "
            f"hours={generation_hours} count={time_plan['generation_time_count']}",
            flush=True,
        )
        print("converting NetCDF to GRIB", flush=True)
    grib_result = generate_current_grib_from_netcdf(
        GenerateCurrentGribRequest(
            bbox=bbox,
            start=generation_start,
            hours=generation_hours,
            step_hours=step_hours,
            output=args.output,
            source="netcdf",
            input_netcdf=download_result.path,
            grid_spacing_deg=args.grid_spacing_deg,
            clip_bbox_to_source=True,
            use_source_grid=True,
            source_grid_regularity_tolerance=source_grid_regularity_tolerance,
        ),
        progress_callback=_generate_copernicus_progress_callback(args.verbose),
    )
    if args.metadata_summary:
        print("validating GRIB stream", flush=True)
    inspection = inspect_grib(grib_result.output)
    result = {
        "download": download_result.as_dict(),
        "grib": grib_result.as_dict(),
        "inspection": inspection,
        "time_plan": _jsonable_time_plan(time_plan),
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"wrote current GRIB: {grib_result.output}", flush=True)
        print(f"validated GRIB stream: {inspection.get('message_count')} messages", flush=True)
        print("complete", flush=True)
    return 0


def _generation_time_plan_from_netcdf(
    path: Path,
    requested_start,
    requested_end,
    step_hours: int,
) -> dict[str, Any]:
    metadata = netcdf_time_metadata(path)
    source_first = metadata["first_time"]
    source_last = metadata["last_time"]
    source_count = int(metadata["time_count"])
    source_step = metadata["step_hours"]
    if requested_end < source_first:
        raise ValidationError(
            f"requested time range ends before downloaded NetCDF begins: requested_end={requested_end.isoformat()}, "
            f"source_first={source_first.isoformat()}"
        )
    if requested_start > source_last:
        raise ValidationError(
            f"requested start is after downloaded NetCDF ends: requested_start={requested_start.isoformat()}, "
            f"source_last={source_last.isoformat()}"
        )

    times = [time for time in metadata["times"] if time >= requested_start and time <= source_last]
    if not times:
        raise ValidationError("downloaded NetCDF contains no usable times at or after requested start")
    generation_start = times[0]
    requested_limit = min(requested_end, source_last)
    if requested_limit < generation_start:
        requested_limit = source_last
    elapsed_hours = int((requested_limit - generation_start).total_seconds() // 3600)
    usable_steps = elapsed_hours // step_hours
    generation_hours = usable_steps * step_hours
    generation_end = generation_start + timedelta(hours=generation_hours)
    generation_count = usable_steps + 1
    if generation_count <= 0:
        raise ValidationError("adjusted generation time range is empty")
    return {
        "source_first_time": source_first,
        "source_last_time": source_last,
        "source_time_count": source_count,
        "source_step_hours": source_step,
        "start": generation_start,
        "end": generation_end,
        "hours": generation_hours,
        "generation_time_count": generation_count,
    }


def _jsonable_time_plan(plan: dict[str, Any]) -> dict[str, Any]:
    result = dict(plan)
    for key in ("source_first_time", "source_last_time", "start", "end"):
        result[key] = result[key].isoformat()
    return result


def _print_mapping(inspection: dict) -> None:
    for key, value in inspection.items():
        if isinstance(value, list):
            if not value:
                print(f"{key}: (none)")
            elif all(isinstance(item, str) for item in value):
                print(f"{key}: {', '.join(value)}")
            else:
                print(f"{key}:")
                for item in value:
                    print(f"  {json.dumps(item, sort_keys=True)}")
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
        input_cache=getattr(args, "input_cache", None),
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
        source_grid_regularity_tolerance=getattr(args, "source_grid_regularity_tolerance", 1e-5),
        tpxo_workers=getattr(args, "tpxo_workers", 1),
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
        "source_label": _source_provenance_label(source),
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


def _download_progress_callback(verbose: bool):
    if not verbose:
        return None

    def callback(step: str, details: dict[str, Any]) -> None:
        if step == "download complete":
            print(f"download complete: {details.get('path', '')}", flush=True)
        elif step:
            print(step, flush=True)

    return callback


def _generate_copernicus_progress_callback(verbose: bool):
    if not verbose:
        return None

    def callback(step: str, details: dict[str, Any]) -> None:
        if step == "generating timestep":
            index = int(details.get("index", 0))
            valid_time = details.get("time", "")
            print(f"wrote {index * 2} messages through valid time {valid_time}", flush=True)
        elif step == "generating GRIB":
            print(f"converting {details.get('steps')} time steps to GRIB", flush=True)
        elif step == "validating GRIB":
            print(f"validated generated stream: {details.get('messages')} messages", flush=True)

    return callback


def _weather_progress_callback(verbose: bool):
    if not verbose:
        return None

    def callback(step: str, details: dict[str, Any]) -> None:
        if step == "checking GFS cycle":
            print(f"checking GFS cycle {details.get('cycle')} f{int(details.get('hour', 0)):03d}", flush=True)
        elif step == "downloading GFS forecast hour":
            print(f"downloading GFS {details.get('cycle')} f{int(details.get('hour', 0)):03d}", flush=True)
        elif step == "downloaded GFS forecast hour":
            print(
                f"downloaded GFS {details.get('cycle')} f{int(details.get('hour', 0)):03d}: "
                f"{details.get('bytes')} bytes",
                flush=True,
            )

    return callback


def _direct_grib_progress_callback(verbose: bool):
    if not verbose:
        return None

    def callback(step: str, details: dict[str, Any]) -> None:
        if step == "downloading Marine Institute GRIB":
            print("downloading Marine Institute Ireland current GRIB", flush=True)
        elif step == "download complete":
            print(
                f"downloaded GRIB: {details.get('output')} "
                f"({details.get('message_count')} messages, {details.get('byte_count')} bytes cleaned, "
                f"{details.get('raw_byte_count')} bytes raw, {details.get('skipped_byte_count')} skipped)",
                flush=True,
            )

    return callback


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sensitive_values = [
        os.environ.get("CURRENTGRIB_TEST_COPERNICUS_USERNAME", ""),
        os.environ.get("CURRENTGRIB_TEST_COPERNICUS_PASSWORD", ""),
        os.environ.get("CURRENTGRIB_COPERNICUS_PASSWORD", ""),
        getattr(args, "username", "") or "",
    ]
    debug = bool(getattr(args, "debug", False))
    verbose = bool(getattr(args, "verbose", False))
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO if verbose else logging.WARNING, force=True)
    redacting_filter = RedactingLogFilter(sensitive_values)
    logging.getLogger().addFilter(redacting_filter)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redacting_filter)
    noisy_loggers = ("urllib3", "botocore", "boto3", "s3transfer", "zarr", "findlibs", "fsspec")
    if debug:
        for noisy in noisy_loggers:
            logging.getLogger(noisy).setLevel(logging.DEBUG)
    else:
        for noisy in noisy_loggers:
            logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return int(args.func(args))
    except TidalCurrentGribError as exc:
        LOGGER.debug("command failed", exc_info=True)
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
