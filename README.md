# tidal-current-grib-generator

`tidal-current-grib-generator` is a command-line tool for generating tidal or ocean-current GRIB files for coastal routing workflows. The first implementation writes GRIB1 current-component fields from a deterministic synthetic source so OpenCPN GRIB compatibility can be tested without relying on external model data.

It creates current data only. It does not create wind, pressure, waves, or other weather fields.

## Why this exists

OpenCPN Weather Routing can use current vectors when current data is present in the loaded GRIB data. A practical workflow is:

1. Generate a current GRIB with this tool.
2. Download or prepare a normal weather/wind GRIB.
3. Use the OpenCPN GRIB plugin's `Merge GRIBs...` utility to merge current and weather files.
4. Run Weather Routing with currents enabled.

This project does not modify OpenCPN.

## Install

From a checkout:

```bash
python -m pip install -e '.[dev]'
```

Writing GRIB files requires ECMWF ecCodes and the Python bindings:

```bash
python -m pip install -e '.[grib]'
```

The Python package alone is not always enough; many systems also need the native ecCodes library installed through the OS package manager.

TPXO/pyTMD support is optional:

```bash
python -m pip install -e '.[tpxo]'
```

Local NetCDF current-file support is optional:

```bash
python -m pip install -e '.[netcdf]'
```

For both GRIB writing and TPXO prediction:

```bash
python -m pip install -e '.[all,dev]'
```

## Generate a synthetic current GRIB

```bash
tidal-current-grib generate \
  --bbox -7.0 51.5 -4.0 55.5 \
  --start 2026-07-01T00:00:00Z \
  --hours 72 \
  --step-hours 1 \
  --grid-spacing-deg 0.0333333 \
  --source synthetic \
  --output irish_sea_current_test.grb \
  --metadata-summary
```

Use `--dry-run` to print the planned grid, times, and message count without writing a file.

## Inspect output

With ecCodes tools installed:

```bash
grib_ls irish_sea_current_test.grb
grib_dump -O -p edition,indicatorOfParameter,Ni,Nj,dataDate,dataTime,P1 irish_sea_current_test.grb
```

The writer validates the GRIB message stream after writing by checking message boundaries and `7777` terminators.

## Data sources

Built-in sources:

- `synthetic`: deterministic rotary tide-like test field.
- `constant`: simple constant eastward current for tests.
- `tpxo` / `pytmd`: pyTMD-backed astronomical tidal-current prediction from local user-supplied model files.
- `netcdf`: local NetCDF current files, including Copernicus Marine files with u/v current components.

Do not scrape, embed, redistribute, or derive open output from proprietary Admiralty, UKHO, or TotalTide atlas data. Users may use their own legally obtained reference data for private validation.

## Generate from local TPXO data

TPXO files must be obtained separately under suitable licence terms. See [docs/tpxo_pytmd_setup.md](docs/tpxo_pytmd_setup.md).

```bash
tidal-current-grib inspect-source \
  --source tpxo \
  --model-dir /path/to/model/root \
  --model-name TPXO10-atlas-v2-nc

tidal-current-grib sample-point \
  --source tpxo \
  --model-dir /path/to/model/root \
  --model-name TPXO10-atlas-v2-nc \
  --lat 53.3 \
  --lon -5.0 \
  --time 2026-07-01T12:00:00Z

tidal-current-grib generate \
  --bbox -7.0 51.5 -4.0 55.5 \
  --start 2026-07-01T00:00:00Z \
  --hours 72 \
  --step-hours 1 \
  --grid-spacing-deg 0.0333333 \
  --source tpxo \
  --model-dir /path/to/model/root \
  --model-name TPXO10-atlas-v2-nc \
  --output irish_sea_tpxo_current.grb \
  --metadata-summary
```

The pyTMD backend uses `pyTMD.compute.tide_currents`, which returns zonal and meridional tidal-current velocities in cm/s. The generator converts those to internal m/s u/v components before writing the same OpenCPN-compatible GRIB1 current fields as the synthetic source.

## Generate from a local Copernicus Marine NetCDF file

See [docs/copernicus_netcdf_setup.md](docs/copernicus_netcdf_setup.md).

```bash
tidal-current-grib inspect-netcdf \
  --input-netcdf ~/OpenCPN/current-data/copernicus/irish_sea_bristol_channel_north_channel_currents_20260701_72h.nc

tidal-current-grib generate \
  --bbox -8.5 50.5 -2.5 56.5 \
  --start 2026-07-01T00:00:00Z \
  --hours 72 \
  --step-hours 1 \
  --grid-spacing-deg 0.03 \
  --source netcdf \
  --input-netcdf ~/OpenCPN/current-data/copernicus/irish_sea_bristol_channel_north_channel_currents_20260701_72h.nc \
  --clip-bbox-to-source \
  --use-source-grid \
  --output irish_sea_bristol_channel_north_channel_copernicus_current.grb \
  --metadata-summary
```

Useful diagnostics:

```bash
tidal-current-grib inspect-grib irish_sea_bristol_channel_north_channel_copernicus_current.grb

tidal-current-grib generate ... --json-summary --dry-run
```

`--coverage-tolerance-deg` allows small differences between requested bbox edges and source coordinate centres. `--clip-bbox-to-source` clips the output bbox to source coordinates. `--use-source-grid` writes on the native NetCDF coordinate centres to avoid unnecessary interpolation. `--source-grid-regularity-tolerance` controls how much float precision noise is allowed before native-grid mode falls back to an error.

## Reference comparison

```bash
tidal-current-grib compare-reference \
  --source synthetic \
  --reference-csv examples/reference_points.example.csv \
  --output comparison.csv
```

The example CSV contains synthetic placeholder values only. Replace it with validation points you have rights to use.

## Safety and limitations

Generated current GRIBs are for planning and experimentation. They are not official navigation products. Local tidal races, overfalls, harbour entrances, wind-driven residuals, storm surge, river flow, and bathymetric effects may not be represented. Mariners must cross-check against official sources and local knowledge.

Accuracy depends entirely on the source model.
