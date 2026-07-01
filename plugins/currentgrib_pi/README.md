# currentgrib_pi

Prototype OpenCPN plugin front-end for `tidal-current-grib-generator`.

This plugin is intentionally a thin GUI wrapper around the Python generator engine. It does not reimplement NetCDF or GRIB writing logic.

The initial asset `data/currentgrib.png` is a project-generated icon asset for this plugin and may be replaced later.

## Status

This is a v1 scaffold:

- toolbar plugin class and dialog source are present
- manual bbox/time/source/output controls are present
- dependency check command is wired
- synthetic/local NetCDF generation command construction is present
- Copernicus password must be entered at runtime and is not stored
- Copernicus download/generate orchestration is documented but not yet fully wired into a background worker

Use the Python CLI directly for production current generation until the plugin build is integrated with an OpenCPN plugin template.
