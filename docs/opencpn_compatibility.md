# OpenCPN compatibility notes

## Scope

This generator creates tidal/ocean-current data only. It does not create wind or weather data, does not replace official navigation products, and does not modify OpenCPN.

Accuracy depends entirely on the source model.

## Current fields

The initial writer targets GRIB1 because existing OpenCPN current examples tested before this project used GRIB1 current components. In those files, current vectors were represented as two scalar component messages:

- parameter 49: eastward/u current component
- parameter 50: northward/v current component

Values are written in metres per second on a regular latitude/longitude surface grid. This project keeps data internally in metres per second and writes those SI component values.

This encoding should be verified against OpenCPN `grib_pi` source and with real generated files. Do not assume every GRIB table used by every producer labels parameters 49 and 50 identically.

## GRIB1 vs GRIB2

GRIB2 has cleaner discipline/category/parameter metadata for oceanographic products, but the first target is OpenCPN compatibility with known current examples. The writer is intentionally modular so a GRIB2 backend can be added later without changing current-source code.

## Testing workflow

1. Generate a synthetic GRIB:

   ```bash
   tidal-current-grib generate \
     --bbox -7.0 51.5 -4.0 55.5 \
     --start 2026-07-01T00:00:00Z \
     --hours 12 \
     --step-hours 1 \
     --grid-spacing-deg 0.05 \
     --source synthetic \
     --output irish_sea_current_test.grb \
     --metadata-summary
   ```

2. Inspect the output with ecCodes:

   ```bash
   grib_ls irish_sea_current_test.grb
   grib_dump -O -p edition,table2Version,indicatorOfParameter,Ni,Nj,dataDate,dataTime,P1 irish_sea_current_test.grb
   ```

3. Load it in OpenCPN's GRIB plugin and confirm current arrows are shown.

4. Use the GRIB plugin `Merge GRIBs...` utility to merge this current GRIB with a weather/wind GRIB.

5. Use Weather Routing with currents enabled.

## Known limitations

- The synthetic source is not real tidal data.
- Only regular latitude/longitude GRIB1 output is implemented.
- GRIB2 output is not implemented yet.
- Land masks are supported in the internal data model but not yet supplied by the synthetic source.
- OpenCPN compatibility still needs direct regression testing with generated files across target OpenCPN versions.
