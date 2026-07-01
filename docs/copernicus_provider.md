# Copernicus provider

## North-West Shelf provider

- Product: `NWSHELF_ANALYSISFORECAST_PHY_004_013`
- Dataset: `cmems_mod_nws_phy-cur_anfc_1.5km-2D_PT1H-i`
- Variables requested:
  - `eastward_sea_water_velocity`
  - `northward_sea_water_velocity`
- Observed downloaded names:
  - `uo`
  - `vo`
- Units: usually `m s-1`
- Resolution: approximately 1.5 km

This is the preferred provider when the requested bbox is fully inside the North-West European shelf coverage used by the provider registry.

## Global provider

- Product: `GLOBAL_ANALYSISFORECAST_PHY_001_024`
- Dataset: `cmems_mod_glo_phy_anfc_0.083deg_PT1H-m`
- Variables requested:
  - `uo`
  - `vo`
- Surface-depth subset:
  - minimum depth `0.0`
  - maximum depth `0.5`
- Resolution: about 0.083 degrees / 1/12 degree
- Default generation step: 1 hour
- Native-grid regularity tolerance: `0.00005` degrees

The global provider covers the rest-of-world fallback path for public use. It is lower resolution than the NWS product and should not be treated as a high-resolution tidal-stream model.

The wider native-grid regularity tolerance handles small float-coordinate precision differences observed in Copernicus Global longitude coordinates without interpolating away from the source grid.

The candidate current-specific 6-hourly dataset `cmems_mod_glo_phy-cur_anfc_0.083deg_PT6H-i` remains a fallback candidate if the hourly physics product is unavailable for a future installation or region.

## Provider selection

`--provider auto` prefers `copernicus_nws` if the bbox is fully inside NWS coverage. Otherwise it selects `copernicus_global` if the bbox is inside the supported global extent.

Explicit `--provider copernicus_global` uses Global even inside NWS coverage.

Antimeridian-crossing bboxes are not supported yet; use a bbox with west longitude less than east longitude.

## Account and terms

Users must have a Copernicus Marine account and comply with Copernicus Marine terms. Register at <https://data.marine.copernicus.eu/register>. This project does not bundle Copernicus data.

## Limitations

Copernicus currents are model data. Accuracy can be reduced near harbour entrances, tidal races, overfalls, drying areas, very shallow water, river mouths, and complex coastlines.
