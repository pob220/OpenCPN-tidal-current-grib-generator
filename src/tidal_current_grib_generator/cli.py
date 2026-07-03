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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tidal_current_grib_generator.errors import TidalCurrentGribError, ValidationError
from tidal_current_grib_generator.copernicus import CopernicusDownloadRequest, download_copernicus_subset
from tidal_current_grib_generator.dependencies import check_dependencies
from tidal_current_grib_generator.geo import BoundingBox, build_regular_grid, build_time_sequence, parse_utc_datetime
from tidal_current_grib_generator.grib.validation import inspect_grib, scan_grib_messages
from tidal_current_grib_generator.grib.merge import merge_grib_files, merge_grib_streams
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
    ECMWF_SOURCE_LABEL,
    ECMWFWeatherRequest,
    CopernicusGlobalWaveRequest,
    GFSWaveRequest,
    GFSWeatherRequest,
    UKMO_UKV_SOURCE_LABEL,
    UKMOUKVInspectRequest,
    UKMOUKVNetCDFInspectRequest,
    UKMOUKVVerifyRequest,
    UKMOUKVWeatherRequest,
    gfs_cycle_candidates,
    generate_gfs_weather_grib,
    generate_copernicus_global_wave_grib,
    generate_ecmwf_weather_grib,
    generate_gfs_wave_grib,
    generate_ukmo_ukv_weather_grib,
    discover_ukmo_ukv_source,
    inspect_ukmo_ukv_netcdf,
    inspect_ukmo_ukv_source,
    list_weather_providers,
    ukmo_ukv_forecast_hour_sequence,
    verify_ukmo_ukv_grib,
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

    inspect_ukv = subparsers.add_parser("inspect-ukv-source", help="Inspect Met Office UKV source availability without writing GRIB.")
    inspect_ukv.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    inspect_ukv.add_argument("--date", help="UKV cycle date YYYYMMDD for explicit cycles.")
    inspect_ukv.add_argument("--cycle", default="auto", help="auto or explicit cycle 00, 06, 12, 18.")
    inspect_ukv.add_argument("--hours", type=int, required=True)
    inspect_ukv.add_argument("--step-hours", type=int, default=1)
    inspect_ukv.add_argument("--weather-grid-spacing-deg", type=float, default=0.025)
    inspect_ukv.add_argument("--max-keys", type=int, default=200, help="Maximum S3 prefixes/objects to inspect.")
    inspect_ukv.add_argument("--refresh-source-index", action="store_true", help="Reserved for future cached source indexes.")
    inspect_ukv.add_argument("--json", action="store_true")
    inspect_ukv.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    inspect_ukv.set_defaults(func=cmd_inspect_ukv_source)

    discover_ukv = subparsers.add_parser("discover-ukv-source", help="Discover Met Office UKV AWS/Open Data object layout.")
    discover_ukv.add_argument("--max-keys", type=int, default=200, help="Maximum S3 prefixes/objects to inspect.")
    discover_ukv.add_argument("--refresh-source-index", action="store_true", help="Reserved for future cached source indexes.")
    discover_ukv.add_argument("--json", action="store_true")
    discover_ukv.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    discover_ukv.set_defaults(func=cmd_discover_ukv_source)

    inspect_ukv_netcdf = subparsers.add_parser("inspect-ukv-netcdf", help="Download and inspect minimal Met Office UKV NetCDF source files.")
    inspect_ukv_netcdf.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    inspect_ukv_netcdf.add_argument("--date", help="UKV cycle date YYYYMMDD for explicit cycles.")
    inspect_ukv_netcdf.add_argument("--cycle", default="auto", help="auto or explicit cycle 00, 03, 06, 09, 12, 15, 18, 21.")
    inspect_ukv_netcdf.add_argument("--hours", type=int, required=True)
    inspect_ukv_netcdf.add_argument("--step-hours", type=int, default=1)
    inspect_ukv_netcdf.add_argument("--download-directory", type=Path, required=True)
    inspect_ukv_netcdf.add_argument("--weather-grid-spacing-deg", type=float, default=0.025)
    inspect_ukv_netcdf.add_argument("--max-keys", type=int, default=400)
    inspect_ukv_netcdf.add_argument("--refresh", action="store_true")
    inspect_ukv_netcdf.add_argument("--extract-sample", action="store_true")
    inspect_ukv_netcdf.add_argument("--json", action="store_true")
    inspect_ukv_netcdf.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    inspect_ukv_netcdf.set_defaults(func=cmd_inspect_ukv_netcdf)

    verify_ukv = subparsers.add_parser("verify-ukv-grib", help="Verify a generated UKV GRIB against regridded UKV NetCDF source values.")
    verify_ukv.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    verify_ukv.add_argument("--date", help="UKV cycle date YYYYMMDD for explicit cycles; auto uses the GRIB reference time.")
    verify_ukv.add_argument("--cycle", default="auto", help="auto or explicit cycle 00, 03, 06, 09, 12, 15, 18, 21.")
    verify_ukv.add_argument("--hours", type=int, required=True)
    verify_ukv.add_argument("--step-hours", type=int, default=1)
    verify_ukv.add_argument("--weather-grid-spacing-deg", type=float, default=0.025)
    verify_ukv.add_argument("--grib", type=Path, required=True)
    verify_ukv.add_argument("--download-directory", type=Path, required=True)
    verify_ukv.add_argument("--tolerance", type=float, default=0.05)
    verify_ukv.add_argument("--refresh", action="store_true")
    verify_ukv.add_argument("--json", action="store_true")
    verify_ukv.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    verify_ukv.set_defaults(func=cmd_verify_ukv_grib)

    generate_weather = subparsers.add_parser("generate-weather", help="Download/generate a weather GRIB.")
    generate_weather.add_argument(
        "--provider",
        choices=["gfs", "gfs_wave", "copernicus_global_waves", "ukmo_ukv", "ecmwf_ifs_open", "dwd_icon_eu"],
        required=True,
    )
    generate_weather.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    generate_weather.add_argument("--start", help="UTC start time; required for Copernicus Global Waves.")
    generate_weather.add_argument("--date", help="GFS cycle date YYYYMMDD for explicit cycles.")
    generate_weather.add_argument("--cycle", help="auto or explicit cycle such as 00, 03, 06, 09, 12, 15, 18, or 21.")
    generate_weather.add_argument("--hours", type=int, required=True)
    generate_weather.add_argument("--step-hours", type=int, default=3)
    generate_weather.add_argument("--weather-preset", choices=["minimal", "routing", "marine"], default="routing")
    generate_weather.add_argument("--weather-grid-spacing-deg", type=float, default=0.025)
    generate_weather.add_argument("--download-directory", type=Path)
    generate_weather.add_argument("--username")
    generate_weather.add_argument("--password-env", default="CURRENTGRIB_COPERNICUS_PASSWORD")
    generate_weather.add_argument("--username-env", default="CURRENTGRIB_TEST_COPERNICUS_USERNAME")
    generate_weather.add_argument("--output", type=Path, required=True)
    generate_weather.add_argument("--overwrite", action="store_true")
    generate_weather.add_argument("--dry-run", action="store_true")
    generate_weather.add_argument("--metadata-summary", action="store_true")
    generate_weather.add_argument("--json", action="store_true")
    generate_weather.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    generate_weather.set_defaults(func=cmd_generate_weather)

    merge_gribs = subparsers.add_parser("merge-gribs", help="Merge current and weather GRIB streams.")
    merge_gribs.add_argument("--current", type=Path, required=True)
    merge_gribs.add_argument("--weather", type=Path, required=True)
    merge_gribs.add_argument("--output", type=Path, required=True)
    merge_gribs.add_argument("--overwrite", action="store_true")
    merge_gribs.add_argument("--metadata-summary", action="store_true")
    merge_gribs.add_argument("--json", action="store_true")
    merge_gribs.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    merge_gribs.set_defaults(func=cmd_merge_gribs)

    environment = subparsers.add_parser("generate-environment-grib", help="Generate a ready merged weather/current GRIB.")
    environment.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    environment.add_argument("--start", help="UTC start time for generated current sources; defaults to weather cycle if weather is generated.")
    environment.add_argument("--date", help="Weather cycle date YYYYMMDD for explicit cycles.")
    environment.add_argument("--cycle", default="auto", help="auto or explicit cycle such as 00, 03, 06, 09, 12, 15, 18, or 21.")
    environment.add_argument("--hours", type=int, required=True)
    environment.add_argument("--step-hours", type=int, default=3)
    environment.add_argument(
        "--weather-provider",
        choices=["none", "existing-file", "gfs", "ukmo_ukv", "ecmwf_ifs_open", "dwd_icon_eu"],
        default="gfs",
    )
    environment.add_argument("--weather-file", type=Path)
    environment.add_argument("--weather-preset", choices=["minimal", "routing", "marine"], default="routing")
    environment.add_argument("--weather-grid-spacing-deg", type=float, default=0.025)
    environment.add_argument("--include-waves", action="store_true")
    environment.add_argument("--wave-provider", choices=["gfs_wave", "copernicus_global_waves"], default="gfs_wave")
    environment.add_argument("--wave-step-hours", type=int)
    environment.add_argument(
        "--current-source",
        choices=[
            "none",
            "existing-file",
            "tpxo-cache",
            "tpxo",
            "marine_ie_irish_sea",
            "copernicus_nws",
            "copernicus_global",
            "auto",
        ],
        default="existing-file",
    )
    environment.add_argument("--current-file", type=Path)
    environment.add_argument("--input-cache", type=Path)
    environment.add_argument("--auto-prepare-tpxo-cache", action="store_true")
    environment.add_argument("--model-dir", "--model-directory", dest="model_directory", type=Path)
    environment.add_argument("--model-name", default=DEFAULT_TPXO_MODEL)
    environment.add_argument("--definition-file", type=Path)
    environment.add_argument("--grid-spacing-deg", type=float, default=0.05)
    environment.add_argument("--download-directory", type=Path)
    environment.add_argument("--username")
    environment.add_argument("--password-env", default="CURRENTGRIB_COPERNICUS_PASSWORD")
    environment.add_argument("--username-env", default="CURRENTGRIB_TEST_COPERNICUS_USERNAME")
    environment.add_argument("--output", type=Path, required=True)
    environment.add_argument("--overwrite", action="store_true")
    environment.add_argument("--keep-intermediate", action="store_true")
    environment.add_argument("--metadata-summary", action="store_true")
    environment.add_argument("--json", action="store_true")
    environment.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    environment.set_defaults(func=cmd_generate_environment_grib)

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
            print(f"{provider['id']}: {provider['label']} ({'implemented' if provider.get('implemented', True) else 'stub'})")
            print(f"  {provider['account']}")
            print(f"  {provider['format']}")
            print(f"  source: {provider['source']}")
    return 0


def cmd_inspect_ukv_source(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    inspection = inspect_ukmo_ukv_source(
        UKMOUKVInspectRequest(
            bbox=bbox,
            hours=args.hours,
            step_hours=args.step_hours,
            cycle=args.cycle,
            date=args.date,
            weather_grid_spacing_deg=args.weather_grid_spacing_deg,
            max_keys=args.max_keys,
            refresh_source_index=args.refresh_source_index,
        )
    )
    if args.json:
        print(json.dumps(inspection, indent=2, sort_keys=True))
    else:
        print(f"provider: {inspection['provider']}", flush=True)
        print(f"source: {inspection['source']}", flush=True)
        print(f"status: {inspection['status']}", flush=True)
        print(f"implemented: {inspection['implemented']}", flush=True)
        print(f"selected_cycle: {inspection['selected_cycle']}", flush=True)
        print(f"source_bucket: {inspection['source_bucket'] or '(not discovered)'}", flush=True)
        print(f"source_region: {inspection['source_region']}", flush=True)
        print(f"anonymous_listing: {inspection['anonymous_listing']}", flush=True)
        if inspection.get("listing_error"):
            print(f"listing_error: {inspection['listing_error']}", flush=True)
        print(f"top_level_prefixes: {inspection['top_level_prefixes'] or '(none)'}", flush=True)
        print(f"likely_ukv_prefixes: {inspection['likely_ukv_prefixes'] or '(none)'}", flush=True)
        print(f"available_model_runs: {inspection['available_model_runs'] or '(not discovered)'}", flush=True)
        print(f"source_paths_or_urls: {inspection['source_paths_or_urls'] or '(none)'}", flush=True)
        print(f"requested_forecast_hours: {inspection['requested_forecast_hours']}", flush=True)
        print(f"available_forecast_hours: {inspection['available_forecast_hours'] or '(not discovered)'}", flush=True)
        print(f"available_near_surface_variables: {inspection['available_near_surface_variables'] or '(not discovered)'}", flush=True)
        print(f"coordinate_variables: {inspection['coordinate_variables'] or '(not discovered)'}", flush=True)
        print(f"grid_mapping: {inspection['grid_mapping'] or '(not discovered)'}", flush=True)
        print(f"source_grid_shape: {inspection['source_grid_shape'] or '(not discovered)'}", flush=True)
        print(f"source_lat_lon_coverage: {inspection['source_lat_lon_coverage'] or '(not discovered)'}", flush=True)
        print(f"bbox_intersects_domain: {inspection['bbox_intersects_domain']}", flush=True)
        print("candidate_variables:", flush=True)
        for key, values in inspection["candidate_variables"].items():
            print(f"  {key}: {values or '(not discovered)'}", flush=True)
        print("candidate_files:", flush=True)
        for item in inspection["candidate_files"][:20]:
            print(f"  {item['key']} ({item['size']} bytes)", flush=True)
        print(f"blocker: {inspection['blocker']}", flush=True)
    return 0


def cmd_discover_ukv_source(args: argparse.Namespace) -> int:
    discovery = discover_ukmo_ukv_source(max_keys=args.max_keys)
    if args.json:
        print(json.dumps(discovery, indent=2, sort_keys=True))
    else:
        print(f"bucket: s3://{discovery['bucket']}/", flush=True)
        print(f"region: {discovery['region']}", flush=True)
        print(f"anonymous_listing: {discovery['anonymous_listing']}", flush=True)
        if discovery.get("error"):
            print(f"error: {discovery['error']}", flush=True)
        print("top_level_prefixes:", flush=True)
        for prefix in discovery["top_level_prefixes"]:
            print(f"  {prefix}", flush=True)
        print("likely_ukv_prefixes:", flush=True)
        for prefix in discovery["likely_ukv_prefixes"]:
            print(f"  {prefix}", flush=True)
        print(f"object_count_seen: {discovery['object_count_seen']}", flush=True)
        print("candidate_files:", flush=True)
        for item in discovery["candidate_files"][:50]:
            print(f"  {item['key']} ({item['size']} bytes)", flush=True)
    return 0


def cmd_inspect_ukv_netcdf(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    inspection = inspect_ukmo_ukv_netcdf(
        UKMOUKVNetCDFInspectRequest(
            bbox=bbox,
            hours=args.hours,
            step_hours=args.step_hours,
            cycle=args.cycle,
            date=args.date,
            download_directory=args.download_directory,
            weather_grid_spacing_deg=args.weather_grid_spacing_deg,
            max_keys=args.max_keys,
            refresh=args.refresh,
            extract_sample=args.extract_sample,
        )
    )
    if args.json:
        print(json.dumps(inspection, indent=2, sort_keys=True))
    else:
        print(f"provider: {inspection['provider']}", flush=True)
        print(f"source: {inspection['source']}", flush=True)
        print(f"status: {inspection['status']}", flush=True)
        print(f"implemented: {inspection['implemented']}", flush=True)
        print(f"selected_cycle: {inspection['selected_cycle']}", flush=True)
        print(f"download_directory: {inspection['download_directory']}", flush=True)
        print("downloaded_files:", flush=True)
        for field, info in inspection["downloaded_files"].items():
            reused = "reused" if info["reused"] else "downloaded"
            print(f"  {field}: {info['path']} ({info['size']} bytes, {reused})", flush=True)
        print("files:", flush=True)
        for field, info in inspection["files"].items():
            print(f"  {field}:", flush=True)
            print(f"    primary_data_variable: {info['primary_data_variable']}", flush=True)
            print(f"    dimensions: {info['dimensions']}", flush=True)
            print(f"    coordinate_variables: {info['coordinate_variables']}", flush=True)
            print(f"    data_variables: {info['data_variables']}", flush=True)
            print(f"    grid_type: {info['grid_type']}", flush=True)
            print(f"    grid_mapping_name: {info['grid_mapping_name']}", flush=True)
            print(f"    lat_lon: {info['lat_lon']}", flush=True)
            print(f"    xy: {info['xy']}", flush=True)
            print(f"    time: {info['time']}", flush=True)
            print(f"    bbox_index_bounds: {info['bbox_index_bounds']}", flush=True)
            print(f"    sample_stats: {info['sample_stats']}", flush=True)
        print("coordinate_summary:", flush=True)
        print(json.dumps(inspection["coordinate_summary"], indent=2, sort_keys=True), flush=True)
        print("time_summary:", flush=True)
        print(json.dumps(inspection["time_summary"], indent=2, sort_keys=True), flush=True)
        print("variable_mappings:", flush=True)
        print(json.dumps(inspection["variable_mappings"], indent=2, sort_keys=True), flush=True)
        print("wind_direction_convention:", flush=True)
        print(json.dumps(inspection["wind_direction_convention"], indent=2, sort_keys=True), flush=True)
        if inspection.get("wind_uv_sample_stats"):
            print("wind_uv_sample_stats:", flush=True)
            print(json.dumps(inspection["wind_uv_sample_stats"], indent=2, sort_keys=True), flush=True)
        if inspection.get("regrid_sample"):
            print("regrid_sample:", flush=True)
            print(json.dumps(inspection["regrid_sample"], indent=2, sort_keys=True), flush=True)
        print("crop_feasibility:", flush=True)
        print(json.dumps(inspection["crop_feasibility"], indent=2, sort_keys=True), flush=True)
        print(f"generation_enabled: {inspection['generation_enabled']}", flush=True)
        print(f"blocker: {inspection['blocker']}", flush=True)
    return 0


def cmd_verify_ukv_grib(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    result = verify_ukmo_ukv_grib(
        UKMOUKVVerifyRequest(
            bbox=bbox,
            grib=args.grib,
            hours=args.hours,
            step_hours=args.step_hours,
            cycle=args.cycle,
            date=args.date,
            download_directory=args.download_directory,
            weather_grid_spacing_deg=args.weather_grid_spacing_deg,
            tolerance=args.tolerance,
            refresh=args.refresh,
        )
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"provider: {result['provider']}", flush=True)
        print(f"source: {result['source']}", flush=True)
        print(f"grib: {result['grib']}", flush=True)
        print(f"selected_cycle: {result['selected_cycle']}", flush=True)
        print(f"forecast_hours: {result['forecast_hours']}", flush=True)
        print(f"message_count: {result['message_count']}", flush=True)
        print(f"expected_message_count: {result['expected_message_count']}", flush=True)
        print(f"grid_checks: {json.dumps(result['grid_checks'], sort_keys=True)}", flush=True)
        print("comparisons:", flush=True)
        for key, comparison in result["comparisons"].items():
            print(
                f"  {key}: max_abs_error={comparison['max_abs_error']:.6g}, "
                f"rmse={comparison['rmse']:.6g}, mean_bias={comparison['mean_bias']:.6g}, "
                f"source_range=({comparison['source_min']:.6g}, {comparison['source_max']:.6g}), "
                f"grib_range=({comparison['grib_min']:.6g}, {comparison['grib_max']:.6g})",
                flush=True,
            )
        print(f"passed: {result['passed']}", flush=True)
    return 0


def cmd_generate_weather(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    if args.provider == "gfs":
        source_label = "NOAA GFS 0.25° forecast via NOMADS"
        request = GFSWeatherRequest(
            bbox=bbox,
            output=args.output,
            hours=args.hours,
            step_hours=args.step_hours,
            cycle=_required_weather_cycle(args),
            date=args.date,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            preset=args.weather_preset,
        )
        generate_func = generate_gfs_weather_grib
    elif args.provider == "gfs_wave":
        source_label = "NOAA GFS Wave forecast via NOMADS"
        request = GFSWaveRequest(
            bbox=bbox,
            output=args.output,
            hours=args.hours,
            step_hours=args.step_hours,
            cycle=_required_weather_cycle(args),
            date=args.date,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        generate_func = generate_gfs_wave_grib
    elif args.provider == "copernicus_global_waves":
        source_label = "Copernicus Marine Global Waves forecast"
        start = _copernicus_wave_start_from_args(args)
        username = args.username or os.environ.get(args.username_env)
        password = os.environ.get(args.password_env)
        if not username:
            raise ValidationError("--username or username environment variable is required for Copernicus Global Waves")
        if not password:
            raise ValidationError(f"{args.password_env} is required for Copernicus Global Waves")
        request = CopernicusGlobalWaveRequest(
            bbox=bbox,
            output=args.output,
            start=start,
            hours=args.hours,
            step_hours=args.step_hours,
            username=username,
            password=password,
            download_directory=args.download_directory,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            grid_spacing_deg=args.weather_grid_spacing_deg,
        )
        generate_func = generate_copernicus_global_wave_grib
    elif args.provider == "ecmwf_ifs_open":
        source_label = ECMWF_SOURCE_LABEL
        request = ECMWFWeatherRequest(
            bbox=bbox,
            output=args.output,
            hours=args.hours,
            step_hours=args.step_hours,
            cycle=_required_weather_cycle(args),
            date=args.date,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            preset=args.weather_preset,
        )
        generate_func = generate_ecmwf_weather_grib
    elif args.provider == "ukmo_ukv":
        source_label = UKMO_UKV_SOURCE_LABEL
        request = UKMOUKVWeatherRequest(
            bbox=bbox,
            output=args.output,
            hours=args.hours,
            step_hours=args.step_hours,
            cycle=_required_weather_cycle(args),
            date=args.date,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
            preset=args.weather_preset,
            weather_grid_spacing_deg=args.weather_grid_spacing_deg,
        )
        generate_func = generate_ukmo_ukv_weather_grib
    elif args.provider == "dwd_icon_eu":
        raise ValidationError(
            "DWD ICON-EU provider is not implemented yet; ECMWF Open Data and GFS are available in this CLI build"
        )
    else:
        raise ValidationError(f"unsupported weather provider: {args.provider}")
    if args.metadata_summary or args.dry_run:
        print(f"Source: {source_label}", flush=True)
        print(f"provider: {args.provider}", flush=True)
        print(f"bbox: {bbox.west},{bbox.south},{bbox.east},{bbox.north}", flush=True)
        print(f"hours: {args.hours}", flush=True)
        print(f"step_hours: {args.step_hours}", flush=True)
        if args.provider == "ukmo_ukv":
            ukv_hours = ukmo_ukv_forecast_hour_sequence(args.hours, args.step_hours)
            print(f"actual_ukv_weather_forecast_hours: {','.join(str(hour) for hour in ukv_hours)}", flush=True)
            if args.step_hours == 1 and args.hours > 54:
                print("UKV weather fields are hourly to 54h and 3-hourly thereafter.", flush=True)
        print(f"output: {args.output.expanduser()}", flush=True)
    result = generate_func(
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
            for warning in getattr(result, "warnings", None) or []:
                print(f"warning: {warning}", flush=True)
    return 0


def cmd_merge_gribs(args: argparse.Namespace) -> int:
    if args.metadata_summary:
        print("merging GRIB streams", flush=True)
        print(f"current: {args.current.expanduser()}", flush=True)
        print(f"weather: {args.weather.expanduser()}", flush=True)
        print(f"output: {args.output.expanduser()}", flush=True)
        print("order: current first, weather second", flush=True)
    result = merge_grib_files(args.current, args.weather, args.output, overwrite=args.overwrite)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"wrote merged GRIB: {result.output}", flush=True)
        print(f"current input messages: {result.current_message_count}", flush=True)
        print(f"weather input messages: {result.weather_message_count}", flush=True)
        print(f"output messages: {result.output_message_count}", flush=True)
        print(f"validated GRIB stream: {result.output_message_count} messages, {result.byte_count} bytes", flush=True)
        inspection = result.inspection
        if inspection.get("edition_counts"):
            print(f"edition counts: {inspection.get('edition_counts')}", flush=True)
        if inspection.get("parameter_counts"):
            print(f"parameter counts: {inspection.get('parameter_counts')}", flush=True)
        if inspection.get("short_name_counts"):
            print(f"short name counts: {inspection.get('short_name_counts')}", flush=True)
        if inspection.get("parameter_names"):
            print(f"parameter names: {inspection.get('parameter_names')}", flush=True)
        if inspection.get("current_component_counts"):
            print(f"current components: {inspection.get('current_component_counts')}", flush=True)
        if inspection.get("first_valid_time") or inspection.get("last_valid_time"):
            print(
                f"valid time range: {inspection.get('first_valid_time')} to {inspection.get('last_valid_time')}",
                flush=True,
            )
    return 0


def cmd_generate_environment_grib(args: argparse.Namespace) -> int:
    bbox = BoundingBox.from_values(args.bbox)
    output = args.output.expanduser()
    if output.exists() and output.is_dir():
        raise ValidationError("--output must be a file path, not a directory")
    if output.exists() and not args.overwrite:
        raise ValidationError(f"output already exists: {output}; use --overwrite to replace it")
    if args.weather_provider == "none" and args.current_source == "none":
        raise ValidationError("at least one of weather or current must be enabled")
    if args.include_waves and args.wave_provider == "gfs_wave" and args.weather_provider not in {"gfs"}:
        raise ValidationError("--include-waves with --wave-provider gfs_wave is currently supported only with --weather-provider gfs")
    wave_step_hours = args.wave_step_hours if args.wave_step_hours is not None else 3
    if args.include_waves and (wave_step_hours < 3 or wave_step_hours % 3 != 0):
        warnings_for_validation = (
            f"requested wave step {wave_step_hours}h is not supported by the selected wave provider; using 3h"
        )
        wave_step_hours = 3
    else:
        warnings_for_validation = None

    temp_parent = output.parent
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp_dir_obj = None
    if args.keep_intermediate:
        temp_dir = Path(tempfile.mkdtemp(prefix=output.stem + ".intermediate.", dir=temp_parent))
    else:
        temp_dir_obj = tempfile.TemporaryDirectory(prefix=output.stem + ".intermediate.", dir=temp_parent)
        temp_dir = Path(temp_dir_obj.name)

    inputs: list[tuple[str, Path]] = []
    intermediates: dict[str, str] = {}
    warnings: list[str] = []
    if warnings_for_validation:
        warnings.append(warnings_for_validation)
    weather_cycle_time: str | None = None
    try:
        if args.metadata_summary:
            print("generating environmental GRIB", flush=True)
            print(f"bbox: {bbox.west},{bbox.south},{bbox.east},{bbox.north}", flush=True)
            print(f"hours: {args.hours}", flush=True)
            print(f"step_hours: {args.step_hours}", flush=True)
            print(f"weather_provider: {args.weather_provider}", flush=True)
            print(f"weather_preset: {args.weather_preset}", flush=True)
            if args.weather_provider == "ukmo_ukv":
                ukv_hours = ukmo_ukv_forecast_hour_sequence(args.hours, args.step_hours)
                print(f"actual_ukv_weather_forecast_hours: {','.join(str(hour) for hour in ukv_hours)}", flush=True)
                if args.step_hours == 1 and args.hours > 54:
                    print("UKV weather fields are hourly to 54h and 3-hourly thereafter.", flush=True)
                    print(f"current_forecast_hours: {','.join(str(hour) for hour in range(0, args.hours + 1, args.step_hours))}", flush=True)
            print(f"include_waves: {bool(args.include_waves)}", flush=True)
            if args.include_waves:
                print(f"wave_provider: {args.wave_provider}", flush=True)
                print(f"wave_step_hours: {wave_step_hours}", flush=True)
                if args.step_hours != wave_step_hours:
                    print(
                        f"Wave fields will be included every {wave_step_hours} hours; "
                        f"weather/current fields remain every {args.step_hours} hour"
                        f"{'' if args.step_hours == 1 else 's'}.",
                        flush=True,
                    )
            print(f"current_source: {args.current_source}", flush=True)
            print(f"output: {output}", flush=True)

        current_source = _resolve_environment_current_source(args.current_source, bbox, args.hours)
        if current_source != args.current_source and args.metadata_summary:
            print(f"selected current provider: {current_source}", flush=True)

        if current_source == "existing-file":
            if args.current_file is None:
                raise ValidationError("--current-file is required with --current-source existing-file")
            current_path = args.current_file.expanduser()
            scan_grib_messages(current_path)
            inputs.append(("current", current_path))
            intermediates["current"] = str(current_path)
        elif current_source in {
            "tpxo-cache",
            "tpxo",
            "marine_ie_irish_sea",
            "copernicus_nws",
            "copernicus_global",
        }:
            # Generated after weather so an omitted --start can align to the
            # selected weather cycle, while merge order still puts current first.
            pass

        if args.weather_provider == "existing-file":
            if args.weather_file is None:
                raise ValidationError("--weather-file is required with --weather-provider existing-file")
            weather_path = args.weather_file.expanduser()
            scan_grib_messages(weather_path)
            inputs.append(("weather", weather_path))
            intermediates["weather"] = str(weather_path)
        elif args.weather_provider in {"gfs", "ukmo_ukv", "ecmwf_ifs_open"}:
            weather_output = temp_dir / f"weather_{args.weather_provider}.grb2"
            if args.weather_provider == "gfs":
                if args.metadata_summary:
                    print("generating GFS atmosphere weather GRIB", flush=True)
                if args.include_waves and args.wave_provider == "gfs_wave" and args.cycle == "auto":
                    weather_result, wave_result = _generate_gfs_environment_with_waves(
                        bbox=bbox,
                        weather_output=weather_output,
                        wave_output=temp_dir / "weather_gfs_wave.grb2",
                        hours=args.hours,
                        step_hours=args.step_hours,
                        wave_step_hours=wave_step_hours,
                        preset=args.weather_preset,
                        metadata_summary=args.metadata_summary,
                        verbose=args.verbose,
                    )
                else:
                    weather_result = generate_gfs_weather_grib(
                        GFSWeatherRequest(
                            bbox=bbox,
                            output=weather_output,
                            hours=args.hours,
                            step_hours=args.step_hours,
                            cycle=args.cycle,
                            date=args.date,
                            overwrite=True,
                            preset=args.weather_preset,
                        ),
                        progress_callback=_weather_progress_callback(args.verbose or args.metadata_summary),
                    )
                    wave_result = None
            elif args.weather_provider == "ukmo_ukv":
                if args.metadata_summary:
                    print("generating Met Office UKV weather GRIB", flush=True)
                weather_result = generate_ukmo_ukv_weather_grib(
                    UKMOUKVWeatherRequest(
                        bbox=bbox,
                        output=weather_output,
                        hours=args.hours,
                        step_hours=args.step_hours,
                        cycle=args.cycle,
                        date=args.date,
                        overwrite=True,
                        preset=args.weather_preset,
                        weather_grid_spacing_deg=args.weather_grid_spacing_deg,
                    ),
                    progress_callback=_weather_progress_callback(args.verbose or args.metadata_summary),
                )
            else:
                if args.metadata_summary:
                    print("generating ECMWF Open Data weather GRIB", flush=True)
                    print("warning: ECMWF Open Data output is not spatially cropped yet; files may be large.", flush=True)
                warnings.append("ECMWF Open Data output is not spatially cropped yet; files may be large.")
                weather_result = generate_ecmwf_weather_grib(
                    ECMWFWeatherRequest(
                        bbox=bbox,
                        output=weather_output,
                        hours=args.hours,
                        step_hours=args.step_hours,
                        cycle=args.cycle,
                        date=args.date,
                        overwrite=True,
                        preset=args.weather_preset,
                    ),
                    progress_callback=_weather_progress_callback(args.verbose or args.metadata_summary),
                )
                warnings.extend(weather_result.warnings or [])
            weather_cycle_time = weather_result.cycle.cycle_time
            inputs.append(("weather", weather_output))
            intermediates["weather"] = str(weather_output)

            if args.include_waves:
                wave_output = temp_dir / f"weather_{args.wave_provider}.grb2"
                if wave_result is None:
                    if args.metadata_summary:
                        print(f"generating wave GRIB provider: {args.wave_provider}", flush=True)
                    if args.wave_provider == "gfs_wave":
                        wave_result = generate_gfs_wave_grib(
                            GFSWaveRequest(
                                bbox=bbox,
                                output=wave_output,
                                hours=args.hours,
                                step_hours=wave_step_hours,
                                cycle=weather_result.cycle.cycle if weather_result.cycle.cycle != "auto" else args.cycle,
                                date=weather_result.cycle.date if weather_result.cycle.date != "auto" else args.date,
                                overwrite=True,
                            ),
                            progress_callback=_weather_progress_callback(args.verbose or args.metadata_summary),
                        )
                    elif args.wave_provider == "copernicus_global_waves":
                        wave_start = parse_utc_datetime(_environment_current_start(args.start, weather_cycle_time))
                        username = args.username or os.environ.get(args.username_env)
                        password = os.environ.get(args.password_env)
                        if not username:
                            raise ValidationError("--username or username environment variable is required for Copernicus Global Waves")
                        if not password:
                            raise ValidationError(f"{args.password_env} is required for Copernicus Global Waves")
                        wave_result = generate_copernicus_global_wave_grib(
                            CopernicusGlobalWaveRequest(
                                bbox=bbox,
                                output=wave_output,
                                start=wave_start,
                                hours=args.hours,
                                step_hours=wave_step_hours,
                                username=username,
                                password=password,
                                download_directory=(args.download_directory / "waves") if args.download_directory else temp_dir / "wave_downloads",
                                overwrite=True,
                                grid_spacing_deg=args.weather_grid_spacing_deg,
                            ),
                            progress_callback=_weather_progress_callback(args.verbose or args.metadata_summary),
                        )
                    else:
                        raise ValidationError(f"unsupported wave provider: {args.wave_provider}")
                inputs.append(("waves", wave_output))
                intermediates["waves"] = str(wave_output)
        elif args.weather_provider == "dwd_icon_eu":
            raise ValidationError("DWD ICON-EU provider is not implemented yet")

        if current_source in {
            "tpxo-cache",
            "tpxo",
            "marine_ie_irish_sea",
            "copernicus_nws",
            "copernicus_global",
        }:
            current_output = temp_dir / f"current_{current_source}.grb"
            current_start = _environment_current_start(args.start, weather_cycle_time)
            _generate_environment_current_source(
                args,
                current_source=current_source,
                bbox=bbox,
                start=current_start,
                output=current_output,
                temp_dir=temp_dir,
            )
            inputs.insert(0, ("current", current_output))
            intermediates["current"] = str(current_output)

        if not inputs:
            raise ValidationError("no GRIB inputs were generated or selected")
        if args.metadata_summary:
            print("merging environmental GRIB streams", flush=True)
            print("order: current first, weather second, waves third", flush=True)
        result = merge_grib_streams(inputs, output, overwrite=True)
        data = {
            "output": str(output),
            "intermediate_files": intermediates,
            "input_message_counts": result.input_message_counts,
            "output_message_count": result.output_message_count,
            "byte_count": result.byte_count,
            "inspection": result.inspection,
            "warnings": warnings,
            "intermediate_directory": str(temp_dir) if args.keep_intermediate else None,
        }
        if args.json:
            print(json.dumps(data, indent=2, sort_keys=True))
        else:
            print(f"wrote environmental GRIB: {output}", flush=True)
            for label, count in result.input_message_counts.items():
                print(f"{label} messages: {count}", flush=True)
            print(f"output messages: {result.output_message_count}", flush=True)
            print(f"validated GRIB stream: {result.output_message_count} messages, {result.byte_count} bytes", flush=True)
            inspection = result.inspection
            if inspection.get("edition_counts"):
                print(f"edition counts: {inspection.get('edition_counts')}", flush=True)
            if inspection.get("short_name_counts"):
                print(f"short name counts: {inspection.get('short_name_counts')}", flush=True)
            if inspection.get("current_component_counts"):
                print(f"current components: {inspection.get('current_component_counts')}", flush=True)
            if inspection.get("first_valid_time") or inspection.get("last_valid_time"):
                print(
                    f"valid time range: {inspection.get('first_valid_time')} to {inspection.get('last_valid_time')}",
                    flush=True,
                )
            if args.keep_intermediate:
                print(f"kept intermediate directory: {temp_dir}", flush=True)
            for warning in warnings:
                print(f"warning: {warning}", flush=True)
    finally:
        if temp_dir_obj is not None:
            temp_dir_obj.cleanup()
    return 0


def _environment_current_start(start_text: str | None, weather_cycle_time: str | None) -> str:
    if start_text:
        return parse_utc_datetime(start_text).isoformat().replace("+00:00", "Z")
    if weather_cycle_time and weather_cycle_time != "autoTauto00Z":
        raw = weather_cycle_time
        if raw.endswith("Z") and "T" in raw:
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[9:11]}:00:00Z"
    now = monotonic_time_datetime_utc_hour()
    return now.isoformat().replace("+00:00", "Z")


def _required_weather_cycle(args: argparse.Namespace) -> str:
    if args.cycle is None:
        raise ValidationError(f"--cycle is required with --provider {args.provider}")
    return str(args.cycle)


def _copernicus_wave_start_from_args(args: argparse.Namespace) -> datetime:
    if args.start:
        return parse_utc_datetime(args.start)
    if args.cycle in (None, "auto"):
        now = datetime.now(timezone.utc)
        rounded_hour = now.hour - (now.hour % 3)
        return now.replace(hour=rounded_hour, minute=0, second=0, microsecond=0)
    if args.date is None:
        raise ValidationError("--date YYYYMMDD is required when --cycle is explicit")
    return parse_utc_datetime(f"{args.date}T{args.cycle}:00:00Z")


def _generate_gfs_environment_with_waves(
    *,
    bbox: BoundingBox,
    weather_output: Path,
    wave_output: Path,
    hours: int,
    step_hours: int,
    wave_step_hours: int,
    preset: str,
    metadata_summary: bool,
    verbose: bool,
):
    progress = _weather_progress_callback(verbose or metadata_summary)
    probe = GFSWeatherRequest(
        bbox=bbox,
        output=weather_output,
        hours=hours,
        step_hours=step_hours,
        cycle="auto",
        overwrite=True,
        preset=preset,
    )
    errors: list[str] = []
    for candidate in gfs_cycle_candidates(probe):
        date = candidate.date
        cycle = candidate.cycle
        if metadata_summary:
            print(f"checking combined GFS atmosphere/wave cycle {candidate.cycle_time}", flush=True)
        try:
            weather_output.unlink(missing_ok=True)
            wave_output.unlink(missing_ok=True)
            weather_result = generate_gfs_weather_grib(
                GFSWeatherRequest(
                    bbox=bbox,
                    output=weather_output,
                    hours=hours,
                    step_hours=step_hours,
                    cycle=cycle,
                    date=date,
                    overwrite=True,
                    preset=preset,
                ),
                progress_callback=progress,
            )
            wave_result = generate_gfs_wave_grib(
                GFSWaveRequest(
                    bbox=bbox,
                    output=wave_output,
                    hours=hours,
                    step_hours=wave_step_hours,
                    cycle=cycle,
                    date=date,
                    overwrite=True,
                ),
                progress_callback=progress,
            )
            if metadata_summary:
                print(f"selected GFS cycle {candidate.cycle_time}", flush=True)
            return weather_result, wave_result
        except ValidationError as exc:
            weather_output.unlink(missing_ok=True)
            wave_output.unlink(missing_ok=True)
            errors.append(f"{candidate.cycle_time}: {exc}")
            if metadata_summary:
                print(f"GFS cycle {candidate.cycle_time} incomplete; trying previous cycle", flush=True)
    raise ValidationError(
        "No complete GFS cycle was available for the requested atmosphere and wave hours. "
        "Try a shorter duration or explicit older cycle. Tried: " + "; ".join(errors)
    )


def monotonic_time_datetime_utc_hour():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)


def _resolve_environment_current_source(current_source: str, bbox: BoundingBox, hours: int) -> str:
    if current_source != "auto":
        return current_source
    selected = select_best_provider_for_bbox(bbox, duration_hours=hours, registry=ProviderRegistry())
    if selected is None:
        raise ValidationError("no current provider supports the requested bbox/duration")
    if selected.id not in {"marine_ie_irish_sea", "copernicus_nws", "copernicus_global"}:
        raise ValidationError(f"auto selected unsupported environmental current provider: {selected.id}")
    return selected.id


def _generate_environment_current_source(
    args: argparse.Namespace,
    *,
    current_source: str,
    bbox: BoundingBox,
    start: str,
    output: Path,
    temp_dir: Path,
) -> None:
    if current_source == "tpxo-cache":
        if args.input_cache is None:
            raise ValidationError("--input-cache is required with --current-source tpxo-cache")
        cache_path = args.input_cache.expanduser()
        cache_status = _environment_tpxo_cache_status(
            cache_path,
            bbox=bbox,
            grid_spacing_deg=args.grid_spacing_deg,
            model_name=args.model_name,
        )
        if cache_status != "usable":
            if not args.auto_prepare_tpxo_cache:
                raise ValidationError(
                    f"TPXO cache is {cache_status}: {cache_path}. "
                    "Use --auto-prepare-tpxo-cache with --model-dir, --model-name, and --grid-spacing-deg to prepare/update it."
                )
            if args.model_directory is None:
                raise ValidationError("--model-dir is required to auto-prepare a TPXO cache")
            if args.metadata_summary:
                if cache_status == "missing":
                    print(f"TPXO cache missing; preparing cache: {cache_path}", flush=True)
                else:
                    print(f"TPXO cache {cache_status}; updating cache: {cache_path}", flush=True)
            started = monotonic_time.perf_counter()
            prepared = prepare_tpxo_cache(
                bbox=bbox,
                grid_spacing_deg=args.grid_spacing_deg,
                model_directory=args.model_directory,
                model_name=args.model_name,
                output=cache_path,
                definition_file=args.definition_file,
                verbose=args.verbose,
            )
            if args.metadata_summary:
                print(
                    f"prepared TPXO cache in {prepared.preparation_seconds:.2f}s "
                    f"({monotonic_time.perf_counter() - started:.2f}s total): {cache_path}",
                    flush=True,
                )
        elif args.metadata_summary:
            print(f"Using TPXO cache: {cache_path}", flush=True)
        if args.metadata_summary:
            print(f"generating TPXO cache current from {start}", flush=True)
        current_args = _environment_generate_namespace(
            bbox=None,
            start=start,
            hours=args.hours,
            step_hours=args.step_hours,
            grid_spacing_deg=None,
            source="tpxo-cache",
            output=output,
            input_cache=cache_path,
            model_directory=None,
            model_name=DEFAULT_TPXO_MODEL,
            definition_file=None,
            metadata_summary=args.metadata_summary,
            verbose=args.verbose,
        )
        cmd_generate(current_args)
        return

    if current_source == "tpxo":
        if args.model_directory is None:
            raise ValidationError("--model-dir is required with --current-source tpxo")
        if not args.model_name:
            raise ValidationError("--model-name is required with --current-source tpxo")
        if args.metadata_summary:
            print(f"generating direct TPXO current from {start}", flush=True)
        current_args = _environment_generate_namespace(
            bbox=[bbox.west, bbox.south, bbox.east, bbox.north],
            start=start,
            hours=args.hours,
            step_hours=args.step_hours,
            grid_spacing_deg=args.grid_spacing_deg,
            source="tpxo",
            output=output,
            input_cache=None,
            model_directory=args.model_directory,
            model_name=args.model_name,
            definition_file=args.definition_file,
            metadata_summary=args.metadata_summary,
            verbose=args.verbose,
        )
        cmd_generate(current_args)
        return

    if current_source == "marine_ie_irish_sea":
        if args.metadata_summary:
            print("downloading Marine.ie Irish Sea current GRIB", flush=True)
            print("warning: Marine.ie is the latest provider model run; valid time range depends on provider run time.", flush=True)
        download_marine_ie_irish_sea_grib(
            output,
            overwrite=True,
            progress_callback=_direct_grib_progress_callback(args.verbose),
        )
        return

    if current_source in {"copernicus_nws", "copernicus_global"}:
        download_dir = args.download_directory.expanduser() if args.download_directory else temp_dir / "current_downloads"
        if args.metadata_summary:
            print(f"generating Copernicus current provider: {current_source}", flush=True)
        copernicus_args = argparse.Namespace(
            bbox=[bbox.west, bbox.south, bbox.east, bbox.north],
            provider=current_source,
            start=start,
            end=None,
            hours=args.hours,
            step_hours=args.step_hours,
            grid_spacing_deg=0.03,
            source_grid_regularity_tolerance=None,
            download_directory=download_dir,
            download_filename=None,
            output=output,
            username=args.username,
            password_env=args.password_env,
            username_env=args.username_env,
            overwrite=True,
            dry_run=False,
            json=False,
            metadata_summary=args.metadata_summary,
            verbose=args.verbose,
            debug=False,
        )
        cmd_generate_copernicus(copernicus_args)
        return

    raise ValidationError(f"unsupported environmental current source: {current_source}")


def _environment_tpxo_cache_status(
    path: Path,
    *,
    bbox: BoundingBox,
    grid_spacing_deg: float,
    model_name: str,
) -> str:
    if not path.exists():
        return "missing"
    try:
        inspection = validate_tpxo_cache(path)
    except ValidationError:
        return "invalid"
    cached_bbox = inspection.get("bbox") or {}
    try:
        cache_bbox = BoundingBox(
            float(cached_bbox["west"]),
            float(cached_bbox["south"]),
            float(cached_bbox["east"]),
            float(cached_bbox["north"]),
        )
    except Exception:
        return "invalid"
    if (
        abs(cache_bbox.west - bbox.west) > 1e-10
        or abs(cache_bbox.south - bbox.south) > 1e-10
        or abs(cache_bbox.east - bbox.east) > 1e-10
        or abs(cache_bbox.north - bbox.north) > 1e-10
    ):
        return "does not match requested bbox"
    if abs(float(inspection.get("grid_spacing_deg", -1.0)) - grid_spacing_deg) > 1e-12:
        return "does not match requested grid spacing"
    if str(inspection.get("model_name", "")) != model_name:
        return "does not match requested model"
    if inspection.get("stale"):
        return "stale"
    return "usable"


def _environment_generate_namespace(
    *,
    bbox: list[float] | None,
    start: str,
    hours: int,
    step_hours: int,
    grid_spacing_deg: float | None,
    source: str,
    output: Path,
    input_cache: Path | None,
    model_directory: Path | None,
    model_name: str,
    definition_file: Path | None,
    metadata_summary: bool,
    verbose: bool,
) -> argparse.Namespace:
    return argparse.Namespace(
        bbox=bbox,
        start=start,
        hours=hours,
        step_hours=step_hours,
        grid_spacing_deg=grid_spacing_deg,
        source=source,
        model_directory=model_directory,
        model_name=model_name,
        definition_file=definition_file,
        input_cache=input_cache,
        tpxo_workers=1,
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
        clip_bbox_to_source=False,
        use_source_grid=False,
        output=output,
        format="grib1",
        units="mps",
        dry_run=False,
        metadata_summary=metadata_summary,
        json_summary=False,
        verbose=verbose,
    )


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
        elif step == "GFS cycle incomplete":
            print(f"GFS cycle {details.get('cycle')} incomplete: {details.get('error')}; trying previous cycle", flush=True)
        elif step == "selected GFS cycle":
            print(f"selected GFS cycle {details.get('cycle')}", flush=True)
        elif step == "downloading GFS forecast hour":
            print(f"downloading GFS {details.get('cycle')} f{int(details.get('hour', 0)):03d}", flush=True)
        elif step == "downloaded GFS forecast hour":
            print(
                f"downloaded GFS {details.get('cycle')} f{int(details.get('hour', 0)):03d}: "
                f"{details.get('bytes')} bytes",
                flush=True,
            )
        elif step == "checking GFS Wave cycle":
            print(f"checking GFS Wave cycle {details.get('cycle')} f{int(details.get('hour', 0)):03d}", flush=True)
        elif step == "GFS Wave cycle incomplete":
            print(f"GFS Wave cycle {details.get('cycle')} incomplete: {details.get('error')}; trying previous cycle", flush=True)
        elif step == "selected GFS Wave cycle":
            print(f"selected GFS Wave cycle {details.get('cycle')}", flush=True)
        elif step == "downloading GFS Wave forecast hour":
            print(f"downloading GFS Wave {details.get('cycle')} f{int(details.get('hour', 0)):03d}", flush=True)
        elif step == "downloaded GFS Wave forecast hour":
            print(
                f"downloaded GFS Wave {details.get('cycle')} f{int(details.get('hour', 0)):03d}: "
                f"{details.get('bytes')} bytes",
                flush=True,
            )
        elif step == "downloading Copernicus Global Waves NetCDF":
            print(
                f"downloading Copernicus Global Waves NetCDF "
                f"{details.get('start')} to {details.get('end')}",
                flush=True,
            )
        elif step == "downloaded Copernicus Global Waves NetCDF":
            print(f"downloaded Copernicus Global Waves NetCDF: {details.get('path')}", flush=True)
        elif step == "wrote wave forecast hour":
            missing = details.get("missing_percent") or {}
            valid = details.get("valid_cell_count") or {}
            mask_summary = ""
            if missing and valid:
                mask_summary = (
                    "; valid cells "
                    + ", ".join(f"{name}={int(valid.get(name, 0))}" for name in ("swh", "perpw", "dirpw"))
                    + "; missing "
                    + ", ".join(f"{name}={float(missing.get(name, 0.0)):.1f}%" for name in ("swh", "perpw", "dirpw"))
                    + f"; {details.get('missing_encoding')}"
                )
            print(
                f"wrote wave forecast hour f{int(details.get('hour', 0)):03d}: "
                f"{details.get('messages')} messages"
                f"{mask_summary}",
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
