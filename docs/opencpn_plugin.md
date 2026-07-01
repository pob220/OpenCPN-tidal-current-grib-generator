# OpenCPN plugin: Current GRIB Generator

The planned plugin name is `currentgrib_pi`. It is a GUI front-end for the existing Python generator engine.

## What it does

- Lets users choose an area, time range, model source, and output path.
- Downloads Copernicus Marine current data using the user's account.
- Converts local NetCDF u/v current data into OpenCPN-compatible GRIB1 current files.
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
3. Select Auto, Copernicus Marine North-West Shelf, Copernicus Marine Global, Local NetCDF, or Synthetic.
4. Enter Copernicus credentials.
5. Choose output directory and filename.
6. Run generation.
7. Open the generated GRIB in the GRIB plugin.

## Merge with weather GRIB

If the OpenCPN GRIB plugin has `Merge GRIBs...`, merge the generated current GRIB with a weather/wind GRIB. Otherwise load the current GRIB directly to inspect currents, and use weather GRIBs separately.

## Weather Routing

After merging, load the merged GRIB and run Weather Routing with currents enabled. Check that current and weather time ranges overlap.
