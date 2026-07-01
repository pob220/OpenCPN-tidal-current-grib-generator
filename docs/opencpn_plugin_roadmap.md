# OpenCPN plugin roadmap

The long-term target is an OpenCPN Ocean Currents Generator plugin, while keeping this repository useful as a standalone generator and testable Python library.

## Architecture

- The generator library remains source-agnostic.
- OpenCPN owns the GUI and user workflow.
- The plugin supplies route or viewport bounds, start time, duration, and resolution.
- The plugin selects a source:
  - local NetCDF
  - Copernicus-downloaded NetCDF file
  - TPXO/pyTMD local model
  - synthetic test source
- The library writes an OpenCPN-compatible current GRIB.
- The plugin can optionally open the generated current GRIB or call the existing Merge GRIBs workflow.

## Data policy

No proprietary atlas data should be bundled. Do not redistribute Admiralty, UKHO, TotalTide, or similar commercial datasets. Copernicus, TPXO, or other model data must be obtained by the user under appropriate terms.

The generator should not store provider credentials unless a future provider system is deliberately designed with clear consent, local storage rules, and revocation.

## Plugin workflow sketch

1. User selects a route, viewport, or manual bbox.
2. User selects model source and local file/model path.
3. User selects start time, duration, and resolution.
4. Plugin previews coverage, time range, grid size, and estimated message count using JSON metadata.
5. Plugin runs generation with progress callbacks.
6. Plugin validates the GRIB stream.
7. Plugin offers to load or merge the generated current GRIB.

## Library needs before plugin integration

- Stable metadata summary objects.
- Progress callback support.
- Cancellation hook.
- Clear user-facing exceptions.
- Source inspection APIs that do not write output files.
- Compatibility tests using generated GRIB fixtures.
