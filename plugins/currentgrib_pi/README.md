# currentgrib_pi

OpenCPN plugin front-end for `tidal-current-grib-generator`.

This plugin is intentionally a thin GUI wrapper around the Python generator engine. It does not reimplement NetCDF or GRIB writing logic.

The initial asset `data/currentgrib.png` is a project-generated icon asset for this plugin and may be replaced later.

## Status

This is a v1 plugin scaffold:

- toolbar plugin class and dialog source are present
- manual bbox/time/source/output controls are present
- dependency check command is wired
- synthetic/local NetCDF generation is wired through the Python CLI
- Copernicus NWS and Global live generation are wired through the Python CLI
- subprocess execution is asynchronous using wx process events
- Copernicus password must be entered at runtime and is not stored
- Copernicus password is passed only to the helper process environment, not on argv

The plugin remains a thin wrapper around the Python generator. It does not reimplement NetCDF download, conversion, or GRIB writing.
