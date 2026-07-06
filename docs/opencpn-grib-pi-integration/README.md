# Experimental OpenCPN grib_pi integration

This directory contains an experimental patch integrating the Environmental GRIB Generator workflow into OpenCPN's stock grib_pi plugin.

The workflow is:

1. Open grib_pi.
2. Click Generate GRIB.
3. Choose weather, wave, and current providers.
4. tidal-current-grib generates, downloads, converts, and merges the data.
5. The resulting environmental GRIB is opened by grib_pi.
6. Weather Routing can then use the loaded GRIB normally.

This is experimental/source-build work, not a Plugin Catalogue release.

## What the patch adds

The patch adds a Generate GRIB action to grib_pi, alongside the normal Open, Settings, and Download controls.

The OpenCPN-facing UI is C++/wxWidgets inside grib_pi. The provider/conversion engine remains in this repository and is run through:

tidal-current-grib generate-environment-grib

## Current tested providers

Weather:

- NOAA GFS
- NOAA GFS Wave
- ECMWF IFS Open Data
- Met Office UKV 2 km

Waves:

- NOAA GFS Wave
- Copernicus Marine Global Waves

Currents:

- TPXO cache/direct astronomical tidal currents
- Copernicus NWS currents
- Copernicus Global currents
- Marine.ie Irish Sea currents
- NOAA RTOFS Global ocean currents
- NOAA OFS / S-111 coastal currents, currently stub/experimental

## Gulf Stream / RTOFS test

NOAA RTOFS Global currents have been tested over a Gulf Stream / Florida Straits area.

Example command:

tidal-current-grib generate-environment-grib \
  --bbox -81.0 24.0 -70.0 36.0 \
  --cycle auto \
  --hours 72 \
  --step-hours 3 \
  --weather-provider gfs \
  --weather-preset routing \
  --include-waves \
  --wave-provider gfs_wave \
  --wave-step-hours 3 \
  --current-source noaa_rtofs_global \
  --download-directory ~/.opencpn/grib/generated/currentgrib_downloads \
  --output ~/.opencpn/grib/generated/environment_gfs_gfswave_rtofs_gulf_stream_72h.grb \
  --overwrite \
  --metadata-summary \
  --verbose

Expected output includes:

- GFS wind, pressure, and air temperature
- GFS wave fields
- RTOFS current U/V components
- OpenCPN-compatible current parameters 49/50
- one merged environmental GRIB

Focused Florida Straits current-only test:

tidal-current-grib generate-provider \
  --provider noaa_rtofs_global \
  --bbox -81.0 24.0 -77.0 28.0 \
  --cycle auto \
  --hours 48 \
  --step-hours 3 \
  --download-directory ~/.opencpn/grib/generated/currentgrib_downloads \
  --output ~/.opencpn/grib/generated/rtofs_florida_straits_current_48h.grb \
  --overwrite \
  --metadata-summary \
  --verbose

## Applying the patch to OpenCPN

From a clean OpenCPN source tree based on upstream master:

cd ~/src/OpenCPN
git switch -c grib-pi-environmental-generator upstream/master
git apply ~/src/tidal-current-grib-generator/docs/opencpn-grib-pi-integration/grib_pi_environmental_generator.patch

Do not apply the patch to a branch where the Environmental GRIB Generator integration is already present.

Build:

cmake --build build --target grib_pi -j2

Or configure from scratch:

cmake -S . -B build -DCMAKE_BUILD_TYPE=RelWithDebInfo -DCMAKE_INSTALL_PREFIX=/usr
cmake --build build --target grib_pi -j2

## Helper setup

cd ~/src/tidal-current-grib-generator
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e '.[grib,tpxo,weather,dev]'

Then verify:

tidal-current-grib providers
tidal-current-grib weather-providers
tidal-current-grib check-dependencies --output-directory ~/.opencpn/grib/generated

## Notes

- This is an input-side workflow for Weather Routing.
- It does not replace Weather Routing.
- It generates one combined environmental GRIB and loads it in grib_pi.
- TPXO is astronomical tide only; it does not model the Gulf Stream.
- Copernicus Global and NOAA RTOFS are model-current providers suitable for ocean-current routing.
- RTOFS currently uses available 6-hourly current guidance.
- Copernicus credentials are passed through environment variables, not command-line passwords.
- Generated GRIB, NetCDF, HDF5, cache, and log files should not be committed.
