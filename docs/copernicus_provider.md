# Copernicus provider

## North-West Shelf v1

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

This is the first implemented live-download provider.

## Global provider

The global Copernicus current provider is present as a registry stub. It is not implemented until an exact dataset is selected, tested, and documented.

## Account and terms

Users must have a Copernicus Marine account and comply with Copernicus Marine terms. This project does not bundle Copernicus data.

## Limitations

Copernicus currents are model data. Accuracy can be reduced near harbour entrances, tidal races, overfalls, drying areas, very shallow water, river mouths, and complex coastlines.
