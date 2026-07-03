# currentgrib_pi

OpenCPN plugin front-end for `tidal-current-grib-generator`.

This plugin is intentionally a thin GUI wrapper around the Python generator engine. It does not reimplement NetCDF or GRIB writing logic.

The initial asset `data/currentgrib.png` is a project-generated icon asset for this plugin and may be replaced later.

## Status

This is a v1 plugin scaffold:

- toolbar plugin class and dialog source are present
- manual bbox/time/source/output controls are present
- dependency check command is wired
- the main dialog is user-facing as Environmental GRIB Generator
- complete weather/current GRIB generation is wired through `tidal-current-grib generate-environment-grib`
- NOAA GFS weather, optional GFS Wave fields, Met Office UKV 2 km weather, ECMWF Open Data, existing weather files, TPXO cache currents, and existing current GRIB files are exposed as CLI-backed workflow choices
- synthetic/local NetCDF generation is wired through the Python CLI
- Copernicus NWS and Global live generation are wired through the Python CLI
- Marine Institute Ireland ready-made Irish Sea current GRIB download is wired
- TPXO10 astronomical tidal-current generation from local licensed model files is wired through the Python CLI
- TPXO cache preparation and cached generation are wired for repeated generation over the same bbox/grid
- subprocess execution is asynchronous using wx process events
- Copernicus password must be entered at runtime and is not stored
- Copernicus password is passed only to the helper process environment, not on argv

The plugin remains a thin wrapper around the Python generator. It does not reimplement NetCDF download, conversion, or GRIB writing.

TPXO output is labelled:

```text
Source: TPXO10 astronomical tide model
```

TPXO predicts astronomical tidal currents from local licensed model files. It does not include weather-driven surge, wind residual currents, river flow, or operational forecast-model corrections.

TPXO cache files are local derived data from licensed TPXO files. Do not redistribute them unless the TPXO licence permits it.
