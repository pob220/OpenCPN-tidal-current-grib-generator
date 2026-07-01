# Copernicus Marine NetCDF setup

## Scope

This project reads local NetCDF current files and converts u/v current components to the existing OpenCPN-compatible GRIB1 current format. It does not download data, store Copernicus credentials, or call Copernicus Marine services.

Users must comply with Copernicus Marine terms and the terms of any dataset they download.

## Product used for Irish Sea testing

- Product: `NWSHELF_ANALYSISFORECAST_PHY_004_013`
- Dataset: `cmems_mod_nws_phy-cur_anfc_1.5km-2D_PT1H-i`
- Variables:
  - `eastward_sea_water_velocity`
  - `northward_sea_water_velocity`

These are modelled ocean currents. They are not official navigation products and do not replace official sources or local knowledge.

Successful medium-area OpenCPN test:

- Requested bbox: `-8.5 50.5 -2.5 56.5`
- Downloaded coordinate centres: approximately `-8.484848.. -2.515151..`, `50.5..56.5`
- Time range: `2026-07-01T00:00:00` to `2026-07-04T00:00:00`
- Downloaded variable names observed in the NetCDF file: `uo`, `vo`

## Example download command

```bash
copernicusmarine subset \
  --dataset-id cmems_mod_nws_phy-cur_anfc_1.5km-2D_PT1H-i \
  --minimum-longitude -8.5 \
  --maximum-longitude -2.5 \
  --minimum-latitude 50.5 \
  --maximum-latitude 56.5 \
  --start-datetime 2026-07-01T00:00:00 \
  --end-datetime 2026-07-04T00:00:00 \
  --variable eastward_sea_water_velocity \
  --variable northward_sea_water_velocity \
  --output-directory ~/OpenCPN/current-data/copernicus \
  --output-filename irish_sea_bristol_channel_north_channel_currents_20260701_72h.nc
```

## Install NetCDF support

```bash
python -m pip install -e '.[netcdf,grib]'
```

On some platforms, `netCDF4` may require native HDF5/NetCDF libraries. If that is inconvenient, xarray can also open many NetCDF files through other installed backends such as `h5netcdf`.

## Inspect the file

```bash
tidal-current-grib inspect-netcdf \
  --input-netcdf ~/OpenCPN/current-data/copernicus/irish_sea_bristol_channel_north_channel_currents_20260701_72h.nc
```

The command prints dimensions, coordinate variables, likely u/v variables, units, time range, latitude/longitude range, and depth levels when present.

## Generate an OpenCPN current GRIB

```bash
tidal-current-grib generate \
  --bbox -7.0 51.5 -4.0 55.5 \
  --start 2026-07-01T00:00:00Z \
  --hours 72 \
  --step-hours 1 \
  --grid-spacing-deg 0.0333333 \
  --source netcdf \
  --input-netcdf ~/OpenCPN/current-data/copernicus/irish_sea_bristol_channel_north_channel_currents_20260701_72h.nc \
  --output irish_sea_bristol_channel_north_channel_copernicus_current.grb \
  --metadata-summary
```

For maximum fidelity on the native Copernicus coordinate centres:

```bash
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
  --output irish_sea_bristol_channel_north_channel_copernicus_native_current.grb \
  --metadata-summary
```

If the requested GRIB grid or times differ from the source grid, the NetCDF source uses xarray spatial interpolation. Exact source times are required by default. Add `--nearest-time` only when selecting the nearest available source time is acceptable.

## Sample one point

```bash
tidal-current-grib sample-point \
  --source netcdf \
  --input-netcdf ~/OpenCPN/current-data/copernicus/irish_sea_bristol_channel_north_channel_currents_20260701_72h.nc \
  --lat 53.3 \
  --lon -5.0 \
  --time 2026-07-01T12:00:00Z
```

## Options

- `--u-variable` and `--v-variable`: override auto-detected current variable names.
- `--lat-variable`, `--lon-variable`, `--time-variable`: override coordinate names.
- `--depth-index`: select a depth/extra dimension by zero-based index.
- `--depth-value`: select nearest depth coordinate value.
- `--assume-units mps|cmps`: use when the NetCDF file lacks reliable units metadata.
- `--nearest-time`: select nearest source time instead of requiring exact time matches.

The internal model remains eastward/northward u/v in metres per second. Diagnostic directions are degrees true, toward which the current flows.

## OpenCPN test checklist

1. Load the generated current GRIB directly in OpenCPN.
2. Confirm the GRIB plugin shows a `Current` checkbox.
3. Confirm current arrows display on the chart.
4. Move the cursor over the region and confirm current speed/direction values are reported.
5. Merge the current GRIB with a weather GRIB using OpenCPN `Merge GRIBs...`.
6. Run Weather Routing with currents enabled.
7. Check SOG/STW differences where the weather/current time ranges overlap.
