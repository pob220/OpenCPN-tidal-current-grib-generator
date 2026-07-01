# TPXO and pyTMD notes

## Summary

`pyTMD` is a Python toolkit for tidal prediction and includes readers/prediction utilities for several tidal model formats, including TPXO-style models. This project uses the documented pyTMD v3 high-level API `pyTMD.compute.tide_currents` for TPXO-style current prediction.

Authoritative project references:

- pyTMD documentation: https://pytmd.readthedocs.io/
- pyTMD source repository: https://github.com/tsutterley/pyTMD

## Model data requirements

TPXO and TPXO10-atlas model files are not bundled with this project. Users must obtain model data separately and comply with the model provider's licence and redistribution terms.

This project must not bundle TPXO data unless the licence explicitly allows it.

## Expected implementation shape

A `PyTMDTPXOSource`:

1. Accept a model directory and model identifier.
2. Optionally accept a pyTMD JSON model definition file.
3. Build arrays of longitude/latitude grid points from the requested regular grid.
4. Call `pyTMD.compute.tide_currents` with `type="grid"`, `crs=4326`, and `standard="datetime"`.
5. Convert pyTMD u/v velocity output from cm/s to m/s.
6. Return a `CurrentGrid` with u/v components in metres per second and a missing-data mask where interpolation fails or the model has no ocean value.

## Coastal limitations

TPXO-style global and regional tide models are model products, not harbour-pilotage products. Resolution, bathymetry, coastline representation, wet/dry masks, and interpolation near complex coastlines can dominate errors. Narrow channels, harbour entrances, tidal races, overfalls, river flow, meteorological residuals, storm surge, and local bathymetric acceleration may not be represented.

## Legal and licensing assumptions

Users are responsible for obtaining model data under appropriate terms. This project should document how to point the software at user-provided model files, but should not hard-code private URLs, credentials, or commercial/regional data.

Admiralty, UKHO, TotalTide, or other proprietary atlas data must not be scraped, embedded, redistributed, or used to derive open output.

## Current status

The repository includes a guarded pyTMD source implementation, but no real model fixture. Tests that require real model data should be run by users with legally obtained local TPXO/TPXO-atlas files.
