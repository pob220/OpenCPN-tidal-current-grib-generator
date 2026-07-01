# Integration testing checklist

## Synthetic GRIB

1. Generate a synthetic GRIB:

   ```bash
   tidal-current-grib generate \
     --bbox -7.0 51.5 -4.0 55.5 \
     --start 2026-07-01T00:00:00Z \
     --hours 12 \
     --step-hours 1 \
     --grid-spacing-deg 0.05 \
     --source synthetic \
     --output synthetic_current.grb \
     --metadata-summary
   ```

2. Load `synthetic_current.grb` in OpenCPN.
3. Confirm the GRIB plugin displays current arrows and current speed/direction.

## TPXO GRIB

1. Inspect local model availability:

   ```bash
   tidal-current-grib inspect-source \
     --source tpxo \
     --model-dir /data/tides \
     --model-name TPXO10-atlas-v2-nc
   ```

2. Sample a point and confirm the values are plausible:

   ```bash
   tidal-current-grib sample-point \
     --source tpxo \
     --model-dir /data/tides \
     --model-name TPXO10-atlas-v2-nc \
     --lat 53.3 \
     --lon -5.0 \
     --time 2026-07-01T12:00:00Z
   ```

3. Generate a TPXO current GRIB:

   ```bash
   tidal-current-grib generate \
     --bbox -7.0 51.5 -4.0 55.5 \
     --start 2026-07-01T00:00:00Z \
     --hours 72 \
     --step-hours 1 \
     --grid-spacing-deg 0.0333333 \
     --source tpxo \
     --model-dir /data/tides \
     --model-name TPXO10-atlas-v2-nc \
     --output tpxo_current.grb \
     --metadata-summary
   ```

4. Confirm OpenCPN displays `tpxo_current.grb` as current data.
5. Download or prepare a weather/wind GRIB.
6. Use OpenCPN GRIB plugin `Merge GRIBs...` to merge current and weather files.
7. Confirm Weather Routing uses current data where spatial coverage and time ranges overlap.
8. Compare selected points against independent references if you have legal access to those references.
