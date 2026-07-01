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
- `pytmd`: documented skeleton for future TPXO/pyTMD integration.

Do not scrape, embed, redistribute, or derive open output from proprietary Admiralty, UKHO, or TotalTide atlas data. Users may use their own legally obtained reference data for private validation.

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
