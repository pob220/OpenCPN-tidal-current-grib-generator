# OpenCPN plugin: Current GRIB Generator

The planned plugin name is `currentgrib_pi`. It is a GUI front-end for the existing Python generator engine.

## What it does

- Lets users choose an area, time range, model source, and output path.
- Downloads Copernicus Marine current data using the user's account.
- Downloads Marine Institute Ireland's ready-made Irish Sea current GRIB.
- Converts local NetCDF u/v current data into OpenCPN-compatible GRIB1 current files.
- Generates TPXO10 astronomical tidal-current GRIBs from local licensed model files.
- Validates the generated GRIB stream.
- Guides users to open or merge the current GRIB using OpenCPN's existing GRIB workflow.

The plugin does not modify OpenCPN core and does not replace the GRIB plugin.

## Dependencies

The v1 plugin scaffold expects the Python command to be installed:

```bash
python -m pip install -e '.[netcdf,grib,copernicus]'
```

Use:

```bash
tidal-current-grib check-dependencies --output-directory ~/.opencpn/grib/generated
```

## Build status

`plugins/currentgrib_pi` is a scaffold intended to be integrated into the official OpenCPN plugin template or an OpenCPN source-tree plugin build. It uses wxWidgets controls and follows the internal plugin pattern of adding a toolbar tool and opening a dialog.

The current scaffold is not yet a packaged OpenCPN plugin release.

## Copernicus account

Users enter their Copernicus Marine username/password at runtime. v1 must not store the password. It may remember the username later. Password storage, if ever added, must use an OS keychain/keyring.

## Generate a current GRIB

1. Open the plugin dialog.
2. Enter bbox manually or use chart-view bounds when implemented.
3. Select a generation mode: Forecast/model current GRIB, Tidal stream prediction from local TPXO model, Local NetCDF file, or Synthetic test source.
4. For forecast/model mode, select Auto, Marine Institute Ireland, Copernicus Marine North-West Shelf, or Copernicus Marine Global.
5. Enter Copernicus credentials only for Copernicus providers.
6. For TPXO mode, select the local model directory and model name, then use `Check TPXO model`.
7. Optionally prepare and use a local TPXO cache for repeated generation over the same bbox/grid.
8. Choose output directory and filename.
9. Run generation.
10. Open the generated GRIB in the GRIB plugin.

The plugin uses these source/provenance labels in summaries:

- Source: Marine Institute Ireland Irish Sea forecast/model current
- Source: Copernicus Marine NWS forecast/model current
- Source: Copernicus Marine Global forecast/model current
- Source: TPXO10 astronomical tide model

TPXO predicts astronomical tidal currents from local licensed model files. It does not include weather-driven surge, wind residual currents, river flow, or operational forecast-model corrections.

TPXO cache files are derived from local licensed TPXO model files. Do not redistribute them unless your TPXO licence permits it.

For TPXO output filenames, prefer provenance-clear names such as:

```text
tpxo10_astronomical_tide_current_YYYYMMDD_HHMM.grb
tpxo10_irish_sea_astronomical_tide_current_YYYYMMDD_HHMM.grb
```

## Merge with weather GRIB

If the OpenCPN GRIB plugin has `Merge GRIBs...`, merge the generated current GRIB with a weather/wind GRIB. Otherwise load the current GRIB directly to inspect currents, and use weather GRIBs separately.

## Weather Routing

After merging, load the merged GRIB and run Weather Routing with currents enabled. Check that current and weather time ranges overlap.
