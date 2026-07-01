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
  --output irish_sea_tpxo_current.grb \
  --metadata-summary
```

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
