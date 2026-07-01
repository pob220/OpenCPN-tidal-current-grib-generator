# Marine Institute Ireland provider

## Irish Sea current GRIB

- Provider id: `marine_ie_irish_sea`
- Display name: Marine Institute Ireland Irish Sea currents
- Type: ready-made direct current GRIB
- File: `irish_sea_ms.grb`
- Format: GRIB1
- Current components: parameters `49` and `50`
- Nominal duration: about 72 hours / 73 hourly times
- Approximate coverage:
  - longitude `-6.994..-4.006`
  - latitude `51.506..55.494`

This provider does not convert NetCDF to GRIB. It downloads a ready-made current GRIB, validates the GRIB stream, and moves it atomically to the requested output path after validation succeeds.

## CLI

```bash
tidal-current-grib generate-provider \
  --provider marine_ie_irish_sea \
  --output ~/.opencpn/grib/generated/marine_ie_irish_sea_current.grb \
  --overwrite \
  --metadata-summary \
  --verbose
```

## Auto selection

`generate-provider --provider auto` selects `marine_ie_irish_sea` when the requested bbox is fully inside the Marine Institute Irish Sea coverage and the requested duration is 72 hours or less. Longer requests or areas outside coverage fall through to Copernicus NWS or Global.

## Notes

The file is model data for planning and experimentation. It is not an official navigation product. Mariners must cross-check against official sources and local knowledge.
