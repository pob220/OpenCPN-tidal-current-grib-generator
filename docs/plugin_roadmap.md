# Plugin roadmap

## v1 milestone

- Toolbar icon appears.
- Dialog opens.
- Manual bbox entry works.
- Copernicus NWS provider can be selected.
- User enters username/password at runtime.
- Plugin downloads a small NetCDF subset.
- Plugin converts it using the Python generator engine.
- Plugin validates the GRIB stream.
- Plugin shows output path.
- Generated GRIB loads in OpenCPN as Current.

## Future improvements

- Use current chart-view bounds.
- Use selected route bounds.
- Background worker with cancel support.
- Tighter integration with GRIB plugin `Merge GRIBs...`.
- Automatic opening of generated GRIB if OpenCPN exposes a stable API.
- TPXO/pyTMD local model backend in the plugin UI.
- FES/HAMTIDE providers if licensing allows.
- Copernicus global/regional provider expansion.
- OS keychain support for optional password storage.
- Direct route-bounds and time-window integration with Weather Routing.
