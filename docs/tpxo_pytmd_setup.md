# TPXO/pyTMD setup

## Install optional dependencies

```bash
python -m pip install -e '.[tpxo]'
```

For GRIB writing as well:

```bash
python -m pip install -e '.[all]'
```

ecCodes also requires the native ecCodes library on many platforms.

## Obtain model data legally

TPXO and TPXO-atlas files are not bundled with this project. Obtain them from the model provider or another legitimate distributor under terms that allow your intended use. Do not scrape, embed, redistribute, or derive open datasets from Admiralty, UKHO, TotalTide, or other proprietary atlas products.

pyTMD documentation notes that TPXO OTIS and ATLAS model data may require registration/manual download from the data producers. This project only reads local model files supplied by the user.

## Model directory layout

pyTMD uses a model root directory and model names from its database. For TPXO10-atlas-v2, the documented pyTMD directory is:

```text
<model_path>/TPXO10_atlas_v2
```

Use `<model_path>` as `--model-dir`, not necessarily the final model subdirectory:

```bash
tidal-current-grib inspect-source \
  --source tpxo \
  --model-dir /data/tides \
  --model-name TPXO10-atlas-v2-nc
```

For model layouts not in the pyTMD database, provide a pyTMD JSON definition file:

```bash
tidal-current-grib inspect-source \
  --source tpxo \
  --model-dir /data/tides \
  --definition-file /data/tides/my_tpxo_definition.json
```

## Example commands

Source: TPXO10 astronomical tide model.

TPXO predicts astronomical tidal currents from local licensed model files. It does not include weather-driven surge, wind residual currents, river flow, or operational forecast-model corrections.

Sample one point:

```bash
tidal-current-grib sample-point \
  --source tpxo \
  --model-dir /data/tides \
  --model-name TPXO10-atlas-v2-nc \
  --lat 53.3 \
  --lon -5.0 \
  --time 2026-07-01T12:00:00Z
```

Generate an OpenCPN-loadable current GRIB:

```bash
tidal-current-grib generate \
  --bbox -7.0 51.5 -4.0 55.5 \
  --start 2026-07-01T00:00:00Z \
  --hours 72 \
  --step-hours 1 \
  --grid-spacing-deg 0.0333333 \
  --source tpxo \
  --model-dir /data/tides \
  --model-name TPXO10-atlas-v2-nc \
  --output tpxo10_astronomical_tide_current_20260701_0000.grb \
  --metadata-summary
```

## Local Derived Cache

For repeated generation over the same bbox and grid spacing, use a local TPXO cache. The cache stores interpolated TPXO harmonic-current constants on the output grid, so later generation can predict arbitrary time ranges without reopening and interpolating the full TPXO NetCDF model files.

```bash
tidal-current-grib prepare-tpxo-cache \
  --bbox -8.5 50.5 -2.5 56.5 \
  --grid-spacing-deg 0.05 \
  --model-dir /data/tides \
  --model-name TPXO10-atlas-v2-nc \
  --output tpxo10_irish_sea.tpxocache \
  --metadata-summary \
  --verbose

tidal-current-grib generate \
  --source tpxo-cache \
  --input-cache tpxo10_irish_sea.tpxocache \
  --start 2026-07-01T23:00:00Z \
  --hours 120 \
  --step-hours 1 \
  --output tpxo10_irish_sea_astronomical_tide_current_20260701_2300.grb \
  --metadata-summary \
  --verbose
```

Cache files are derived from local licensed TPXO model files. Do not redistribute cache files unless your TPXO licence permits it. The project `.gitignore` excludes `*.tpxocache`, `*.npz`, and `tpxo-cache/`.

## Conventions

- Internal values are u/v current components in metres per second.
- `u` is eastward current.
- `v` is northward current.
- Diagnostic speed/direction reports direction toward which the current flows, degrees true.
- GRIB output preserves the OpenCPN-compatible GRIB1 current component encoding already validated with OpenCPN.
- The pyTMD backend calls `pyTMD.compute.tide_currents`, documented as returning `u` and `v` velocities in cm/s, and converts cm/s to m/s.
- The backend does not convert transports to velocities. If a model exposes transports only, do not use it until a correct conversion is implemented and validated.

## Known limitations

TPXO-style tidal models provide astronomical tidal currents. They do not include wind-driven residuals, storm-surge residuals, river flow, wave drift, or every local bathymetric/coastal effect.

Coastal resolution can be a limiting factor around narrow channels, harbour entrances, banks, tidal races, overfalls, and drying areas. pyTMD can extrapolate near model boundaries, but extrapolation should be used cautiously in complex coastal water.

No real TPXO fixture is included in this repository because redistribution rights are model-specific.
