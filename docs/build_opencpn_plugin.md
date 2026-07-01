# Building `currentgrib_pi` Against OpenCPN

This project keeps the generator engine as a Python CLI/library and provides an OpenCPN plugin front-end in `plugins/currentgrib_pi`.

The plugin invokes the installed `tidal-current-grib` executable for dependency checks, synthetic generation, local NetCDF conversion, and Copernicus live download/generation. Copernicus passwords are passed only through the child-process environment and are not placed on the command line.

## Development Layout

Assumed local paths:

```sh
~/src/OpenCPN
~/src/tidal-current-grib-generator
```

For development inside the OpenCPN source tree, symlink the plugin into OpenCPN's plugin directory:

```sh
ln -s ~/src/tidal-current-grib-generator/plugins/currentgrib_pi \
  ~/src/OpenCPN/plugins/currentgrib_pi
```

If the symlink already exists, leave it in place. Alternatively, copy the directory, but a symlink keeps edits in this repository visible to the OpenCPN build.

## Configure and Build

Configure OpenCPN so it discovers `currentgrib_pi`:

```sh
cmake -S ~/src/OpenCPN -B ~/src/OpenCPN/build
```

Build just the plugin target:

```sh
cmake --build ~/src/OpenCPN/build --target currentgrib_pi -j2
```

Expected output includes:

```text
*** Added plugin: .../plugins/currentgrib_pi
[100%] Built target currentgrib_pi
```

The shared library is written to:

```text
~/src/OpenCPN/build/plugins/currentgrib_pi/libcurrentgrib_pi.so
```

The build also copies `currentgrib.png` to development data locations used by OpenCPN:

```text
~/src/OpenCPN/build/plugins/currentgrib_pi/data/currentgrib.png
~/src/OpenCPN/build/share/plugins/currentgrib_pi/data/currentgrib.png
```

On Linux development builds, CMake also installs imported plugin metadata to:

```text
~/.opencpn/plugins/install_data/imports/currentgrib_pi.xml
```

OpenCPN's plugin manager uses this metadata for non-system plugins. Without it, a locally loaded plugin can be opened as a shared library but still be pruned from `Options -> Plugins` when it is disabled/unloaded and has no catalog entry. Disable this copy step with:

```sh
cmake -S ~/src/OpenCPN -B ~/src/OpenCPN/build -DCURRENTGRIB_INSTALL_DEV_METADATA=OFF
```

## Launch OpenCPN With the Development Plugin

From the OpenCPN source tree:

```sh
cd ~/src/OpenCPN
OPENCPN_PLUGIN_DIRS=~/src/OpenCPN/build/plugins/currentgrib_pi ./build/opencpn
```

Expected OpenCPN log lines should show that `libcurrentgrib_pi.so` was found and loaded, followed by the common name `Current GRIB Generator`.

Useful log lines to grep for:

```text
currentgrib_pi: create_pi
currentgrib_pi: constructor
currentgrib_pi: GetCommonName
currentgrib_pi: GetShortDescription
currentgrib_pi: GetLongDescription
currentgrib_pi: GetAPIVersionMajor -> 1
currentgrib_pi: GetAPIVersionMinor -> 18
currentgrib_pi: Init start
currentgrib_pi: toolbar tool id=
```

## Generator Executable Discovery

The plugin looks for `tidal-current-grib` in this order:

1. `TIDAL_CURRENT_GRIB` environment variable, if set.
2. `tidal-current-grib` on `PATH`.
3. `~/src/tidal-current-grib-generator/.venv/bin/tidal-current-grib`.
4. Literal fallback `tidal-current-grib`.

The dialog also exposes a generator executable field so a development path can be set manually.

## Manual Test Checklist

1. Launch OpenCPN with `OPENCPN_PLUGIN_DIRS`.
2. Confirm the `Current GRIB Generator` plugin loads.
3. Confirm the toolbar icon appears.
4. Click the toolbar icon and confirm the `Ocean Current GRIB Generator` dialog opens.
5. Click `Check dependencies` and confirm output from:

   ```sh
   tidal-current-grib check-dependencies --output-directory <selected-output-dir> --json
   ```

6. Select `Synthetic test source` and generate a small GRIB.
7. Load the generated GRIB in OpenCPN's GRIB plugin and confirm it appears as `Current`.
8. Select `Local NetCDF file`, choose a Copernicus current NetCDF file, and generate a GRIB.
9. Load the generated NetCDF-derived GRIB and confirm current arrows and speed/direction values display.
10. Select `Copernicus Marine North-West Shelf high-resolution currents` and generate a tiny live test.
11. Select `Copernicus Marine Global currents` and generate a tiny live test outside NWS coverage.
12. Load the generated Copernicus GRIBs and confirm current arrows and speed/direction values display.
13. Merge the current GRIB with a weather GRIB using the GRIB plugin's merge workflow if available.
14. Run Weather Routing and check that current effects appear where current and weather time ranges overlap.

## Current v1 Limits

- Copernicus generation is delegated to the Python helper process; cancellation sends SIGTERM to the child process tree on Linux.
- Automatic opening of generated GRIBs is best-effort through the GRIB plugin message API. If the GRIB plugin does not accept the request, open the generated file manually.
- Passwords are not stored, not passed on command lines, and not logged by the plugin scaffold.
- The plugin does not modify OpenCPN core and does not bundle Copernicus, Admiralty, UKHO, TotalTide, or other proprietary current data.
