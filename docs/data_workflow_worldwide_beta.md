# Environmental GRIB Data Workflow Toward Worldwide Beta

This project builds OpenCPN-ready environmental GRIBs by combining weather,
waves, and current data from legally usable user-supplied or open model
sources. It does not replace official navigation products.

## Implemented Providers

### Weather

| Provider | Coverage | Account | Format | Notes |
| --- | --- | --- | --- | --- |
| NOAA GFS | Global | No account | GRIB2 from NOMADS | Default provider. Spatially cropped, compact, reliable for worldwide beta. |
| NOAA HRRR 3 km | Contiguous United States | No account | GRIB2 from NOMADS | Implemented and live-smoked. Short-range, hourly updated high-resolution provider. Currently full-grid/uncropped, so files can be large. |
| NOAA GFS Wave | Global | No account | GRIB2 from NOMADS | Significant wave height, primary wave period, primary wave direction. Usually 3-hourly. |
| ECMWF IFS Open Data | Global | No account | GRIB2 | Higher-quality global/medium-range option. Current implementation is not spatially cropped, so files may be large. |
| ECMWF AIFS Open Data | Global | No account | GRIB2 | Experimental/unverified. Live retrieval failed in smoke testing; ECMWF IFS remains the tested ECMWF option. Not spatially cropped when retrieval succeeds. |
| Met Office UKV 2 km | UK/Ireland | No account for AWS/Open Data | NetCDF converted to GRIB2 | High-resolution short-range provider. Hourly to about 54h, then 3-hourly to 120h. |
| DWD ICON-EU 13 km | Europe | No account | GRIB2 from DWD Open Data | Implemented and live-smoked. Regional European forecast. Currently downloads full-domain field files, so files can be large. |

### Currents

| Provider | Coverage | Account | Format | Notes |
| --- | --- | --- | --- | --- |
| Copernicus Marine NWS currents | North-West European Shelf | Copernicus account | NetCDF converted to GRIB1 current fields | High-resolution forecast/model currents for UK/Ireland/North Sea/Celtic Sea region. |
| Copernicus Marine Global currents | Global ocean | Copernicus account | NetCDF converted to GRIB1 current fields | Lower-resolution global forecast/model currents. |
| NOAA RTOFS Global currents | Global ocean model; current generator uses NOAA regional high-value NetCDF domains where available | No account | NetCDF converted to GRIB1 current fields | NOAA operational model currents, useful for offshore/Gulf Stream-type circulation where RTOFS guidance is available. |
| NOAA OFS / S-111 coastal currents | U.S. coastal waters and Great Lakes | No account | S-111/HDF5 | Experimental stub; not yet a complete GRIB generator. |
| Marine.ie Irish Sea currents | Irish Sea | No user account | Ready-made GRIB1 current file | Latest ~3-day Irish Sea provider GRIB, normalized and validated before use. |
| TPXO direct | Global where local licensed model covers | User-supplied licensed model files | Harmonic model converted to GRIB1 currents | Astronomical tidal-current prediction only. |
| TPXO cache | User-prepared local cache | User-supplied licensed model files | Derived local cache converted to GRIB1 currents | Fast repeated astronomical tidal-current generation for a fixed area/grid. Do not redistribute cache unless permitted by the TPXO licence. |
| Existing current GRIB | User supplied | User responsibility | GRIB | Imported and merged without regridding. |

## Provider Gaps Before Worldwide Beta

### 1. Copernicus Global Waves

- Expected value: global wave forecast independent of NOAA, useful when users already have Copernicus credentials for currents.
- Coverage: global ocean.
- Account requirement: Copernicus Marine account.
- Likely data format: Copernicus Marine NetCDF subset, converted to OpenCPN-readable GRIB2 wave fields.
- Likely effort: medium. Download and credential handling can reuse the current Copernicus path; NetCDF-to-GRIB2 wave mapping must be validated.
- Beta status: belongs before Stage 1 beta. Implemented first because it complements global weather/currents.

### 2. NOAA RTOFS Global Currents

- Expected value: no-account global operational ocean currents, useful fallback where Copernicus credentials are not available.
- Coverage: global ocean.
- Account requirement: no account.
- Data format: NOAA/NCEP RTOFS NetCDF source files converted to OpenCPN-compatible GRIB1 current fields 49/50.
- Beta status: implemented for RTOFS regional high-value NetCDF domains; live validation is required before wider testing.

### 3. NOAA OFS / S-111 Regional Coastal Currents

- Expected value: high-value regional coastal currents in US waters.
- Coverage: regional US coastal domains.
- Account requirement: no account expected for NOAA open products.
- Likely data format: model GRIB/NetCDF and/or S-111 products.
- Likely effort: high. Multiple regional models, changing grids, and S-111 ingestion/mapping need careful validation.
- Beta status: experimental stub unless a single OFS/S-111 domain is prioritized and validated.

## Which Current Source Should I Choose?

- TPXO cache: astronomical tidal streams for arbitrary dates from local licensed TPXO model data.
- Copernicus Global: global ocean model currents; should include large-scale circulation such as Gulf Stream flow.
- NOAA RTOFS Global: NOAA global ocean model currents; a no-account candidate for offshore/Gulf Stream routing where RTOFS regional extraction is available.
- Copernicus NWS: high-resolution Northwest European shelf model currents.
- Marine.ie: ready-made Irish Sea model current GRIB.
- NOAA OFS/S-111: U.S. coastal and Great Lakes forecast currents; experimental stub in this build.

### 4. ECMWF AIFS / ECMWF Wave Open Data

- AIFS weather is experimental/unverified in this build. Live retrieval failed during smoke testing using the current ECMWF Open Data request.
- Coverage: global.
- Account requirement: no account for available open data.
- Data format: GRIB2.
- Current limitation: live retrieval still needs validation; spatial cropping is not yet applied when retrieval succeeds, so files may be large.
- ECMWF wave open-data support remains future work.

### 5. MET Norway / Nordic Provider

- Expected value: strong regional provider for Nordic waters and North Atlantic use cases.
- Coverage: Nordic and nearby ocean regions depending on product.
- Account requirement: likely no account for open endpoints.
- Likely data format: GRIB2/NetCDF.
- Likely effort: medium. Need product discovery, projection handling, and field mapping.
- Beta status: after Stage 1 beta unless Nordic coverage becomes a priority.

### 6. DWD ICON / ICON-EU

- ICON-EU weather is implemented for the regular latitude/longitude single-level GRIB2 products.
- Coverage: Europe for ICON-EU. ICON global remains future work.
- Account requirement: no account for DWD Open Data.
- Data format: GRIB2, downloaded as `.bz2` field files and decompressed before merging.
- Current limitation: provider downloads complete requested full-domain field files rather than byte-range bbox subsets, so files can be large.

## Stage 1 Worldwide Beta Acceptance Criteria

Minimum capability:

- Global weather via NOAA GFS.
- Global weather alternatives via ECMWF IFS Open Data; ECMWF AIFS is experimental/unverified until live retrieval is fixed.
- Regional weather via live-smoked NOAA HRRR for the contiguous United States and DWD ICON-EU for Europe, with current full-grid/full-domain file-size limitations.
- Global currents via Copernicus Marine Global currents.
- Global/offshore currents via NOAA RTOFS Global where the requested RTOFS domain is supported.
- Global waves via NOAA GFS Wave or Copernicus Marine Global Waves.
- Regional high-resolution weather via Met Office UKV 2 km.
- TPXO cache for astronomical tidal streams from local licensed TPXO files.
- Existing weather/current GRIB import and merge.
- Clear provider capability summaries in CLI/plugin output.
- Clean failures for unavailable coverage, missing credentials, unsupported time ranges, or missing local model/cache files.
- Generated GRIB opens in OpenCPN.
- Weather Routing computes with merged weather/current GRIBs.

Operational expectations:

- Passwords are not passed on command lines, logged, stored, or written to temp files.
- Copernicus credentials are provided by the user and used only for the current operation.
- Generated NetCDF, GRIB, cache, and log files remain ignored by Git.
- Mixed-cadence GRIBs are documented clearly: weather, waves, and currents may have different valid-time intervals in the same merged stream.
