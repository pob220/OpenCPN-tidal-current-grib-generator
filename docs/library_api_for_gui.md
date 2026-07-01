# Library API for GUI integration

These APIs are the best current boundary for a future GUI or OpenCPN plugin. They are not yet a formal stable public API, but they are intentionally small and suitable for hardening.

## Core types

- `BoundingBox`: west/south/east/north geographic bounds.
- `RegularGrid`: latitude and longitude coordinate arrays plus i/j spacing.
- `build_regular_grid`: create a regular grid from bbox and spacing.
- `build_time_sequence`: create UTC forecast-valid times.
- `CurrentGrid`: u/v current components in metres per second.

## Sources

- `create_source`: source registry for `synthetic`, `constant`, `netcdf`, and `tpxo`.
- `NetCDFCurrentSource`: local NetCDF current files, including Copernicus products.
- `PyTMDTPXOSource`: TPXO/pyTMD local model source.
- `SyntheticRotaryTideSource`: deterministic test data.
- `ProviderRegistry` and `select_best_provider_for_bbox`: source/provider selection.
- `CopernicusDownloadRequest` and `download_copernicus_subset`: live Copernicus download using the Python API.
- `GenerateCurrentGribRequest` and `generate_current_grib_from_netcdf`: GUI-friendly generation boundary.

GUI code should call source inspection methods before generation to show coverage, variables, units, and time ranges.

## GRIB output

- `EccodesGrib1CurrentWriter`: writes the OpenCPN-compatible GRIB1 current component messages.
- `scan_grib_messages`: validates GRIB message starts and `7777` terminators.
- `inspect_grib`: returns stream and optional ecCodes metadata.

## Metadata and progress

The CLI `--json-summary` output is the current reference shape for GUI metadata:

- source
- input/output files
- requested and actual bbox
- source and output grid bounds
- grid size
- time range and step count
- message count
- variables and units
- interpolation/clipping flags
- warnings

The writer accepts a progress callback after each timestep. A future cancellation hook should be added at the generation loop boundary before each source read.

## Error handling

User dialogs should catch:

- `ValidationError`: invalid user input, missing files, coverage/time errors.
- `MissingDependencyError`: optional dependency is not installed.
- `UnsupportedSourceError`: unknown source name.
- `TidalCurrentGribError`: base class for expected user-facing failures.
