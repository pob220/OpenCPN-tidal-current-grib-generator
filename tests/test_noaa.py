from pathlib import Path

from tidal_current_grib_generator.cli import main
from tidal_current_grib_generator.geo import BoundingBox
from tidal_current_grib_generator.noaa import (
    RTOFSCurrentResult,
    RTOFSCycle,
    discover_rtofs_cycle,
    parse_rtofs_inventory,
    rtofs_forecast_hours,
    rtofs_region_for_bbox,
)
from tidal_current_grib_generator.providers import ProviderRegistry, select_best_provider_for_bbox


def _fake_grib1(payload: bytes = b"x") -> bytes:
    length = 12 + len(payload)
    return b"GRIB" + length.to_bytes(3, "big") + bytes([1]) + payload + b"7777"


def test_provider_registry_includes_noaa_rtofs_and_ofs():
    registry = ProviderRegistry()
    rtofs = registry.get("noaa_rtofs_global")
    assert rtofs.implemented is True
    assert rtofs.default_step_hours == 6
    assert rtofs.max_duration_hours == 192
    assert "No account" in rtofs.description

    ofs = registry.get("noaa_ofs_s111")
    assert ofs.implemented is False
    assert ofs.provider_type == "experimental_discovery"


def test_auto_provider_selection_does_not_select_rtofs_by_default():
    provider = select_best_provider_for_bbox(BoundingBox(-81.0, 24.0, -70.0, 36.0), registry=ProviderRegistry())
    assert provider is not None
    assert provider.id != "noaa_rtofs_global"


def test_rtofs_forecast_hours_are_six_hourly_and_limited():
    assert rtofs_forecast_hours(24, 3) == [6, 12, 18, 24]
    assert rtofs_forecast_hours(72, 6)[-1] == 72


def test_rtofs_region_for_bbox():
    assert rtofs_region_for_bbox(BoundingBox(-81.0, 24.0, -70.0, 36.0)) == "US_east"
    assert rtofs_region_for_bbox(BoundingBox(-125.0, 35.0, -122.0, 38.0)) == "US_west"
    assert rtofs_region_for_bbox(BoundingBox(-150.0, 55.0, -145.0, 58.0)) == "US_west"


def test_parse_rtofs_inventory():
    html = """
    <a href="rtofs_glo_3dz_f006_6hrly_hvr_US_east.nc">file</a>
    <a href="rtofs_glo_3dz_f012_6hrly_hvr_US_east.nc">file</a>
    <a href="rtofs_glo_3dz_f006_6hrly_hvr_US_west.nc">file</a>
    """
    assert parse_rtofs_inventory(html) == {
        6: ["US_east", "US_west"],
        12: ["US_east"],
    }


def test_discover_rtofs_cycle_from_fixture_inventory():
    html = """
    rtofs_glo_3dz_f006_6hrly_hvr_US_east.nc
    rtofs_glo_3dz_f012_6hrly_hvr_US_east.nc
    """
    cycle = discover_rtofs_cycle(
        requested_hours=[6, 12],
        region="US_east",
        cycle="00",
        date="20260705",
        opener=lambda url: html,
    )
    assert cycle == RTOFSCycle("20260705", "00")


def test_generate_provider_noaa_rtofs_mocked(monkeypatch, tmp_path: Path, capsys):
    calls = {}

    def fake_generate(**kwargs):
        calls.update(kwargs)
        kwargs["output"].write_bytes(_fake_grib1(b"rtofs"))
        return RTOFSCurrentResult(
            output=kwargs["output"],
            message_count=2,
            byte_count=kwargs["output"].stat().st_size,
            selected_cycle="2026-07-05T00:00:00Z",
            forecast_hours=[6, 12],
            source_files=[tmp_path / "source.nc"],
            summary={"provider": "noaa_rtofs_global"},
        )

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_noaa_rtofs_current_grib", fake_generate)
    monkeypatch.delenv("CURRENTGRIB_COPERNICUS_PASSWORD", raising=False)
    rc = main(
        [
            "generate-provider",
            "--provider",
            "noaa_rtofs_global",
            "--bbox",
            "-81.0",
            "24.0",
            "-70.0",
            "36.0",
            "--cycle",
            "auto",
            "--hours",
            "12",
            "--step-hours",
            "3",
            "--download-directory",
            str(tmp_path),
            "--output",
            str(tmp_path / "rtofs.grb"),
            "--overwrite",
            "--metadata-summary",
        ]
    )
    assert rc == 0
    assert calls["cycle"] == "auto"
    assert calls["step_hours"] == 3
    assert calls["download_directory"] == tmp_path
    assert "credentials: none required" in capsys.readouterr().out


def test_generate_environment_grib_noaa_rtofs_mocked(monkeypatch, tmp_path: Path):
    calls = {}

    def fake_generate(**kwargs):
        calls.update(kwargs)
        kwargs["output"].write_bytes(_fake_grib1(b"rtofs"))
        return RTOFSCurrentResult(
            output=kwargs["output"],
            message_count=2,
            byte_count=kwargs["output"].stat().st_size,
            selected_cycle="2026-07-05T00:00:00Z",
            forecast_hours=[6, 12],
            source_files=[],
            summary={"provider": "noaa_rtofs_global"},
        )

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_noaa_rtofs_current_grib", fake_generate)
    monkeypatch.delenv("CURRENTGRIB_COPERNICUS_PASSWORD", raising=False)
    output = tmp_path / "environment.grb"
    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-81.0",
            "24.0",
            "-70.0",
            "36.0",
            "--weather-provider",
            "none",
            "--current-source",
            "noaa_rtofs_global",
            "--cycle",
            "auto",
            "--hours",
            "12",
            "--step-hours",
            "3",
            "--download-directory",
            str(tmp_path),
            "--output",
            str(output),
            "--metadata-summary",
        ]
    )
    assert rc == 0
    assert output.exists()
    assert calls["bbox"] == BoundingBox(-81.0, 24.0, -70.0, 36.0)
    assert calls["cycle"] == "auto"


def test_noaa_ofs_s111_stub_fails_clearly(tmp_path: Path, capsys):
    rc = main(
        [
            "generate-provider",
            "--provider",
            "noaa_ofs_s111",
            "--output",
            str(tmp_path / "ofs.grb"),
        ]
    )
    assert rc == 2
    assert "experimental stub" in capsys.readouterr().err

