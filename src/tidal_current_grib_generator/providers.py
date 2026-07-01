"""Provider registry and selection logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tidal_current_grib_generator.geo import BoundingBox


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    coverage: BoundingBox | None
    dataset_id: str | None
    variables: tuple[str, ...]
    implemented: bool
    resolution: str
    description: str

    def supports_bbox(self, bbox: BoundingBox) -> bool:
        if self.coverage is None:
            return self.implemented
        return (
            bbox.west >= self.coverage.west
            and bbox.east <= self.coverage.east
            and bbox.south >= self.coverage.south
            and bbox.north <= self.coverage.north
        )

    def as_dict(self) -> dict[str, Any]:
        data = self.__dict__.copy()
        data["coverage"] = self.coverage.__dict__ if self.coverage else None
        return data


COPERNICUS_NWS = Provider(
    id="copernicus_nws",
    label="Copernicus Marine North-West Shelf high-resolution currents",
    coverage=BoundingBox(-20.0, 40.0, 13.0, 65.0),
    dataset_id="cmems_mod_nws_phy-cur_anfc_1.5km-2D_PT1H-i",
    variables=("eastward_sea_water_velocity", "northward_sea_water_velocity"),
    implemented=True,
    resolution="approx 1.5 km",
    description="Modelled North-West European shelf currents including tides/residuals.",
)

COPERNICUS_GLOBAL = Provider(
    id="copernicus_global",
    label="Copernicus Marine Global currents",
    coverage=BoundingBox(-180.0, -80.0, 180.0, 90.0),
    dataset_id=None,
    variables=("uo", "vo"),
    implemented=False,
    resolution="global model resolution depends on selected product",
    description="Global Copernicus fallback is a provider stub until an exact dataset is selected and tested.",
)

LOCAL_NETCDF = Provider(
    id="local_netcdf",
    label="Local NetCDF file",
    coverage=None,
    dataset_id=None,
    variables=(),
    implemented=True,
    resolution="source file native grid or requested output grid",
    description="User-selected local NetCDF current file.",
)

SYNTHETIC = Provider(
    id="synthetic",
    label="Synthetic test source",
    coverage=BoundingBox(-180.0, -90.0, 180.0, 90.0),
    dataset_id=None,
    variables=(),
    implemented=True,
    resolution="requested grid",
    description="Offline deterministic test source.",
)


class ProviderRegistry:
    def __init__(self) -> None:
        self.providers = {
            provider.id: provider
            for provider in (COPERNICUS_NWS, COPERNICUS_GLOBAL, LOCAL_NETCDF, SYNTHETIC)
        }

    def get(self, provider_id: str) -> Provider:
        return self.providers[provider_id]

    def list(self) -> list[Provider]:
        return list(self.providers.values())


def select_best_provider_for_bbox(
    bbox: BoundingBox,
    start: datetime | None = None,
    end: datetime | None = None,
    registry: ProviderRegistry | None = None,
) -> Provider | None:
    _ = (start, end)
    registry = registry or ProviderRegistry()
    nws = registry.get("copernicus_nws")
    if nws.implemented and nws.supports_bbox(bbox):
        return nws
    global_provider = registry.get("copernicus_global")
    if global_provider.implemented and global_provider.supports_bbox(bbox):
        return global_provider
    return None
