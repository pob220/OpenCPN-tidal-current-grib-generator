# tidal-current-grib-generator


## OpenCPN grib_pi integration

This branch now includes an experimental integration path for OpenCPN's stock `grib_pi` plugin.

The integrated workflow is:

1. Open `grib_pi`.
2. Click **Generate GRIB**.
3. Choose weather, wave, and current providers.
4. The `tidal-current-grib` helper generates one combined environmental GRIB.
5. `grib_pi` opens the generated GRIB.
6. Weather Routing can use it normally.

The grib_pi integration is documented here:

`docs/opencpn-grib-pi-integration/`

That directory includes:

- an experimental `grib_pi` patch
- build notes
- helper setup notes
- a Gulf Stream / NOAA RTOFS test case

This is source-build experimental work, not a Plugin Catalogue release.

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

Live Copernicus Marine downloads are optional:

```bash
python -m pip install -e '.[copernicus,netcdf,grib]'
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

Remote providers:

- `marine_ie_irish_sea`: Marine Institute Ireland ready-made Irish Sea current GRIB, about 3 days, no Copernicus login required.
- `copernicus_nws`: North-West Shelf high-resolution currents for the UK/Ireland/North Sea/English Channel/Celtic Sea area, using `NWSHELF_ANALYSISFORECAST_PHY_004_013` / `cmems_mod_nws_phy-cur_anfc_1.5km-2D_PT1H-i`.
- `copernicus_global`: lower-resolution rest-of-world currents, using `GLOBAL_ANALYSISFORECAST_PHY_001_024` / `cmems_mod_glo_phy_anfc_0.083deg_PT1H-m`.
- `noaa_rtofs_global`: NOAA RTOFS Global ocean-current forecast, no account required. Useful for offshore model currents including Gulf Stream-type circulation where RTOFS guidance is available.
- `noaa_ofs_s111`: NOAA OFS/S-111 coastal currents. Experimental stub in this build; not yet a complete GRIB generator.
- `auto`: selects Marine Institute Ireland inside its Irish Sea coverage for up to 72 hours, then NWS, otherwise Global.

| Provider | Best for | Data type | Duration | Login |
| --- | --- | --- | --- | --- |
| Marine Institute Ireland Irish Sea currents | Irish Sea/North Channel where covered | ready-made current GRIB | about 3 days | no Copernicus login |
| Copernicus NWS | UK/Ireland/North Sea/English Channel/Celtic Sea area | NetCDF model currents converted to GRIB | user selected | Copernicus login |
| Copernicus Global | rest-of-world | NetCDF model currents converted to GRIB | user selected | Copernicus login |
| NOAA RTOFS Global | Global ocean model, current implementation uses NOAA regional high-value NetCDF domains where available | NetCDF model currents converted to GRIB | up to about 8 days | no login |
| NOAA OFS/S-111 | U.S. coastal waters and Great Lakes | S-111/HDF5 model products | not implemented | no login |

Which current source should I choose?

- TPXO cache: astronomical tidal streams for arbitrary dates from local licensed TPXO data.
- Copernicus Global: global ocean model currents; should include large-scale circulation such as Gulf Stream flow.
- NOAA RTOFS Global: NOAA global ocean model currents; a no-account candidate for offshore/Gulf Stream routing where RTOFS regional extraction is available.
- Copernicus NWS: high-resolution Northwest European shelf model currents.
- Marine.ie: ready-made Irish Sea model current GRIB.
- NOAA OFS/S-111: U.S. coastal/Great Lakes forecast currents; experimental stub in this build.

Source labels used in CLI summaries and the OpenCPN plugin:

- Source: Marine Institute Ireland Irish Sea forecast/model current
- Source: Copernicus Marine NWS forecast/model current
- Source: Copernicus Marine Global forecast/model current
- Source: NOAA RTOFS Global ocean-current forecast
- Source: TPXO10 astronomical tide model

Register for a Copernicus Marine account at <https://data.marine.copernicus.eu/register>. Users are responsible for complying with Copernicus Marine terms.

Do not scrape, embed, redistribute, or derive open output from proprietary Admiralty, UKHO, or TotalTide atlas data. Users may use their own legally obtained reference data for private validation.

## Generate from local TPXO data

Source: TPXO10 astronomical tide model.

TPXO files must be obtained separately under suitable licence terms. The suggested local layout is `~/OpenCPN/tide-models/TPXO10_atlas_v2`, using `~/OpenCPN/tide-models` as `--model-dir`. See [docs/tpxo_pytmd_setup.md](docs/tpxo_pytmd_setup.md).

TPXO predicts astronomical tidal currents from local licensed model files. It does not include weather-driven surge, wind residual currents, river flow, or operational forecast-model corrections.

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
  --output tpxo10_astronomical_tide_current_20260701_0000.grb \
  --metadata-summary
```

The pyTMD backend uses `pyTMD.compute.tide_currents`, which returns zonal and meridional tidal-current velocities in cm/s. The generator converts those to internal m/s u/v components before writing the same OpenCPN-compatible GRIB1 current fields as the synthetic source.

For repeated TPXO generation over the same bbox/grid, prepare a local derived cache of interpolated harmonic current constants:

```bash
tidal-current-grib prepare-tpxo-cache \
  --bbox -8.5 50.5 -2.5 56.5 \
  --grid-spacing-deg 0.05 \
  --model-dir /path/to/model/root \
  --model-name TPXO10-atlas-v2-nc \
  --output tpxo10_irish_sea.tpxocache \
  --metadata-summary

tidal-current-grib generate \
  --source tpxo-cache \
  --input-cache tpxo10_irish_sea.tpxocache \
  --start 2026-07-01T23:00:00Z \
  --hours 120 \
  --step-hours 1 \
  --output tpxo10_irish_sea_astronomical_tide_current_20260701_2300.grb \
  --metadata-summary
```

TPXO cache files are derived from local licensed TPXO model files. Do not redistribute them unless your TPXO licence permits it. Cache files are ignored by Git by default.

Marine.ie and Copernicus providers are forecast/model current sources. TPXO is a harmonic astronomical tidal-current prediction; use the source label in generated summaries when comparing or merging products.

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

## Generate directly from Copernicus Marine

The command prompts for credentials unless supplied by environment variables. Do not put passwords on the command line.

North-West Shelf:

```bash
tidal-current-grib generate-copernicus \
  --provider copernicus_nws \
  --bbox -8.5 50.5 -2.5 56.5 \
  --start 2026-07-01T00:00:00Z \
  --hours 72 \
  --step-hours 1 \
  --download-directory ~/.opencpn/grib/generated/currentgrib_downloads \
  --output ~/.opencpn/grib/generated/irish_sea_copernicus_current.grb \
  --metadata-summary
```

## Download a ready-made Marine Institute Ireland current GRIB

```bash
tidal-current-grib generate-provider \
  --provider marine_ie_irish_sea \
  --output ~/.opencpn/grib/generated/marine_ie_irish_sea_current.grb \
  --overwrite \
  --metadata-summary \
  --verbose
```

The downloaded file is already an OpenCPN-compatible current GRIB. The command validates the GRIB stream before moving it to the output path.

## Generate directly from NOAA RTOFS Global currents

NOAA RTOFS does not require credentials. The provider selects a complete recent RTOFS cycle, downloads the needed current NetCDF files, and converts u/v components in m/s to OpenCPN-compatible GRIB1 current parameters 49/50.

```bash
tidal-current-grib generate-provider \
  --provider noaa_rtofs_global \
  --bbox -81.0 24.0 -70.0 36.0 \
  --cycle auto \
  --hours 72 \
  --step-hours 3 \
  --download-directory ~/.opencpn/grib/generated/currentgrib_downloads \
  --output ~/.opencpn/grib/generated/rtofs_gulf_stream_current_72h.grb \
  --overwrite \
  --metadata-summary \
  --verbose
```

Global fallback example:

```bash
tidal-current-grib generate-copernicus \
  --provider copernicus_global \
  --bbox -40.5 30.0 -40.0 30.5 \
  --start 2026-07-01T00:00:00Z \
  --hours 6 \
  --step-hours 1 \
  --download-directory ~/.opencpn/grib/generated/currentgrib_downloads \
  --output ~/.opencpn/grib/generated/global_copernicus_current.grb \
  --metadata-summary
```

OpenCPN workflow:

1. Generate a current GRIB using the CLI or `currentgrib_pi`.
2. Download or generate a weather/wind GRIB.
3. Use `tidal-current-grib merge-gribs` or the upgraded GRIB plugin's `Merge GRIBs...` utility to merge current and weather files.
4. Run Weather Routing with currents enabled and overlapping current/weather time ranges.

## Weather GRIB providers

Weather GRIB support is available from the CLI and through the Environmental GRIB Generator plugin wrapper.

```bash
tidal-current-grib weather-providers
```

Implemented providers:

- `gfs`: Source: NOAA GFS 0.25° forecast via NOMADS. Downloads bbox-subset GRIB2 files without credentials.
- `gfs_wave`: Source: NOAA GFS Wave forecast via NOMADS. Downloads bbox-subset significant wave height, primary wave period, and primary wave direction from the GFS Wave global 0.25 degree grid.
- `copernicus_global_waves`: Source: Copernicus Marine Global Waves forecast. Downloads Copernicus Marine Global Ocean Waves NetCDF subsets and converts significant wave height, primary/peak wave period, and wave direction to OpenCPN-oriented GRIB2. Requires a Copernicus Marine account.
- `ukmo_ukv`: Source: Met Office UKV 2 km forecast. Downloads no-account AWS/Open Data NetCDF source files from `s3://met-office-atmospheric-model-data/`, regrids the projected UKV 2 km source to regular latitude/longitude, and writes OpenCPN-oriented GRIB2 weather fields for UK/Ireland short-range use.
- `ecmwf_ifs_open`: Source: ECMWF IFS Open Data forecast. Uses the optional official `ecmwf-opendata` client. The first implementation retrieves the requested fields from ECMWF Open Data and records the bbox in metadata; spatial cropping is not yet applied by this provider.

Experimental/planned providers:

- `dwd_icon_eu`: Source: DWD ICON-EU forecast.

UKV discovery helpers:

```bash
tidal-current-grib discover-ukv-source --max-keys 200 --verbose
```

```bash
tidal-current-grib inspect-ukv-source \
  --cycle auto \
  --bbox -8.5 50.5 -2.5 56.5 \
  --hours 6 \
  --verbose
```

The discovery command lists the public S3 object layout anonymously, equivalent in access model to:

```bash
aws s3 ls --no-sign-request s3://met-office-atmospheric-model-data/
```

The Met Office dataset layout changed around January 2026, so the code validates object structure dynamically instead of hard-coding a brittle path. `inspect-ukv-source` reports discovered prefixes, candidate NetCDF objects, sizes, inferred cycles/forecast hours, and the current blocker instead of producing a fake GRIB.

UKV NetCDF source inspection:

```bash
tidal-current-grib inspect-ukv-netcdf \
  --cycle auto \
  --bbox -8.5 50.5 -2.5 56.5 \
  --hours 6 \
  --download-directory ~/.opencpn/grib/generated/ukv_samples \
  --verbose
```

This downloads only the required NetCDF source files for the requested forecast hours:

- `pressure_at_mean_sea_level.nc`
- `temperature_at_screen_level.nc`
- `wind_speed_at_10m.nc`
- `wind_direction_at_10m.nc`

Use `--extract-sample` to compute data ranges for the requested bbox and run a projected-grid to regular-lon/lat interpolation sample:

```bash
tidal-current-grib inspect-ukv-netcdf \
  --cycle auto \
  --bbox -5.8 53.0 -5.2 53.5 \
  --hours 1 \
  --download-directory ~/.opencpn/grib/generated/ukv_samples \
  --extract-sample \
  --weather-grid-spacing-deg 0.025 \
  --verbose
```

Observed UKV NetCDF metadata shows a projected `lambert_azimuthal_equal_area` x/y grid with CF grid mapping, not a regular lat/lon grid. Wind is published as speed plus `wind_from_direction`; the inspected metadata supports meteorological "from" direction, so the U/V conversion is `u = -speed * sin(direction)` and `v = -speed * cos(direction)`.

Generate UKV weather GRIB:

```bash
tidal-current-grib generate-weather \
  --provider ukmo_ukv \
  --bbox -8.5 50.5 -2.5 56.5 \
  --cycle auto \
  --hours 24 \
  --step-hours 1 \
  --weather-preset routing \
  --weather-grid-spacing-deg 0.025 \
  --output ~/.opencpn/grib/generated/ukmo_ukv_weather_irish_sea_24h.grb \
  --metadata-summary \
  --verbose
```

Verify UKV source-to-GRIB numeric consistency:

```bash
tidal-current-grib verify-ukv-grib \
  --cycle auto \
  --bbox -8.5 50.5 -2.5 56.5 \
  --hours 6 \
  --step-hours 1 \
  --weather-grid-spacing-deg 0.025 \
  --grib ~/.opencpn/grib/generated/ukmo_ukv_weather_irish_sea_24h.grb \
  --download-directory ~/.opencpn/grib/generated/ukv_samples \
  --verbose
```

The OpenCPN plugin should only expose UKV as a normal UI provider after manual OpenCPN display testing confirms the generated wind, pressure, and temperature fields render correctly.

Install the optional ECMWF client with:

```bash
pip install 'tidal-current-grib-generator[weather]'
```

GFS example:

```bash
tidal-current-grib generate-weather \
  --provider gfs \
  --bbox -8.5 50.5 -2.5 56.5 \
  --cycle auto \
  --hours 24 \
  --step-hours 3 \
  --output ~/.opencpn/grib/generated/gfs_weather_irish_sea_24h.grb2 \
  --metadata-summary \
  --verbose
```

GFS marine preset with waves:

```bash
tidal-current-grib generate-environment-grib \
  --bbox -8.5 50.5 -2.5 56.5 \
  --cycle auto \
  --hours 24 \
  --step-hours 3 \
  --weather-provider gfs \
  --weather-preset marine \
  --include-waves \
  --current-source tpxo-cache \
  --input-cache ~/.opencpn/tpxo-cache/tpxo10_irish_sea_bristol_north_channel_0p05.tpxocache \
  --output ~/.opencpn/grib/generated/environment_gfs_waves_tpxo_irish_sea_24h.grb \
  --metadata-summary \
  --verbose
```

GFS weather with Copernicus Global Waves and TPXO cache currents:

```bash
CURRENTGRIB_COPERNICUS_PASSWORD='...' \
tidal-current-grib generate-environment-grib \
  --bbox -8.5 50.5 -2.5 56.5 \
  --cycle auto \
  --hours 72 \
  --step-hours 1 \
  --weather-provider gfs \
  --weather-preset routing \
  --include-waves \
  --wave-provider copernicus_global_waves \
  --wave-step-hours 3 \
  --username your-copernicus-username \
  --current-source tpxo-cache \
  --input-cache ~/.opencpn/tpxo-cache/tpxo10_irish_sea_bristol_north_channel_0p05.tpxocache \
  --output ~/.opencpn/grib/generated/environment_gfs_copernicus_waves_tpxo_irish_sea_72h.grb \
  --metadata-summary \
  --verbose
```

Do not pass the Copernicus password on the command line in normal use; provide it through `CURRENTGRIB_COPERNICUS_PASSWORD` or the plugin password field.

Weather presets:

- `minimal`: 10 m U/V wind only.
- `routing`: 10 m U/V wind, mean sea-level pressure, 2 m temperature.
- `marine`: routing fields plus gusts, precipitation, and cloud cover where available. NOMADS applies variable and level selections independently, so this preset can include a few extra harmless surface/atmosphere messages.

See [docs/data_workflow_worldwide_beta.md](docs/data_workflow_worldwide_beta.md) for the provider-completion roadmap toward a worldwide beta.

ECMWF Open Data example:

```bash
tidal-current-grib generate-weather \
  --provider ecmwf_ifs_open \
  --bbox -8.5 50.5 -2.5 56.5 \
  --cycle auto \
  --hours 72 \
  --step-hours 3 \
  --output ~/.opencpn/grib/generated/ecmwf_ifs_weather_irish_sea_72h.grb2 \
  --metadata-summary \
  --verbose
```

Merge a current GRIB with a weather GRIB:

```bash
tidal-current-grib merge-gribs \
  --current ~/.opencpn/grib/generated/tpxo10_cached_astronomical_tide_current_20260701_2300_120h.grb \
  --weather ~/.opencpn/grib/generated/gfs_weather_irish_sea_24h.grb2 \
  --output ~/.opencpn/grib/generated/merged_cli_gfs_tpxo_irish_sea_test.grb \
  --metadata-summary \
  --verbose
```

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
