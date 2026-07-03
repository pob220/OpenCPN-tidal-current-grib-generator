from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from tidal_current_grib_generator.cli import main
from tidal_current_grib_generator.errors import ValidationError
from tidal_current_grib_generator.grib.merge import merge_grib_files
from tidal_current_grib_generator.geo import BoundingBox
from tidal_current_grib_generator.weather import (
    ECMWFWeatherRequest,
    GFSCycle,
    GFSWaveRequest,
    GFSWeatherRequest,
    UKMOUKVInspectRequest,
    UKMOUKVNetCDFInspectRequest,
    UKMOUKVWeatherRequest,
    WeatherGenerateResult,
    build_gfs_filter_url,
    build_gfs_wave_filter_url,
    discover_ukmo_ukv_source,
    forecast_hour_sequence,
    generate_gfs_wave_grib,
    generate_ecmwf_weather_grib,
    generate_gfs_weather_grib,
    generate_ukmo_ukv_weather_grib,
    gfs_variables_for_preset,
    gfs_cycle_candidates,
    inspect_ukmo_ukv_netcdf,
    list_weather_providers,
    ukmo_ukv_forecast_hour_sequence,
    verify_ukmo_ukv_grib,
    UKMOUKVVerifyRequest,
    wind_speed_direction_to_uv,
)


def _fake_grib2(payload: bytes = b"") -> bytes:
    length = 20 + len(payload)
    return b"GRIB" + b"\x00\x00\x00" + b"\x02" + length.to_bytes(8, "big") + payload + b"7777"


def _fake_grib1(payload: bytes = b"") -> bytes:
    length = 12 + len(payload)
    return b"GRIB" + length.to_bytes(3, "big") + b"\x01" + payload + b"7777"


def test_weather_provider_registry_includes_gfs():
    providers = list_weather_providers()

    by_id = {provider.id: provider for provider in providers}
    assert {"gfs", "ukmo_ukv", "ecmwf_ifs_open", "dwd_icon_eu"} <= set(by_id)
    assert by_id["gfs"].source == "NOAA NOMADS"
    assert by_id["gfs"].format == "GRIB2"
    assert by_id["gfs"].account == "free/no account"
    assert by_id["ecmwf_ifs_open"].source == "ECMWF Open Data"
    assert by_id["ecmwf_ifs_open"].implemented is True
    assert by_id["ukmo_ukv"].source == "Met Office AWS/Open Data"
    assert by_id["ukmo_ukv"].implemented is True
    assert by_id["dwd_icon_eu"].implemented is False


def test_gfs_url_construction_for_known_cycle_bbox():
    url = build_gfs_filter_url(
        GFSCycle("20260701", "00"),
        6,
        BoundingBox(-8.5, 50.5, -2.5, 56.5),
    )

    assert url.startswith("https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl?")
    assert "file=gfs.t00z.pgrb2.0p25.f006" in url
    assert "dir=%2Fgfs.20260701%2F00%2Fatmos" in url
    assert "var_UGRD=on" in url
    assert "var_VGRD=on" in url
    assert "var_PRMSL=on" in url
    assert "var_TMP=on" in url
    assert "lev_10_m_above_ground=on" in url
    assert "lev_mean_sea_level=on" in url
    assert "lev_2_m_above_ground=on" in url
    assert "leftlon=-8.5" in url
    assert "rightlon=-2.5" in url
    assert "toplat=56.5" in url
    assert "bottomlat=50.5" in url


def test_gfs_preset_fields():
    assert set(gfs_variables_for_preset("minimal")) == {"var_UGRD", "var_VGRD", "lev_10_m_above_ground"}
    marine = gfs_variables_for_preset("marine")
    assert marine["var_GUST"] == "on"
    assert marine["var_TCDC"] == "on"
    assert marine["var_APCP"] == "on"


def test_gfs_wave_url_construction_for_known_cycle_bbox():
    url = build_gfs_wave_filter_url(
        GFSCycle("20260701", "06"),
        3,
        BoundingBox(-8.5, 50.5, -2.5, 56.5),
    )

    assert url.startswith("https://nomads.ncep.noaa.gov/cgi-bin/filter_gfswave.pl?")
    assert "file=gfswave.t06z.global.0p25.f003.grib2" in url
    assert "dir=%2Fgfs.20260701%2F06%2Fwave%2Fgridded" in url
    assert "var_HTSGW=on" in url
    assert "var_PERPW=on" in url
    assert "var_DIRPW=on" in url
    assert "lev_surface=on" in url


def test_gfs_auto_cycle_candidates_newest_to_older():
    request = GFSWeatherRequest(
        bbox=BoundingBox(-1, 50, 0, 51),
        output=Path("out.grb2"),
        hours=6,
        cycle="auto",
        max_auto_cycles=3,
    )

    candidates = gfs_cycle_candidates(request, now=datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc))

    assert [(candidate.date, candidate.cycle) for candidate in candidates] == [
        ("20260702", "12"),
        ("20260702", "06"),
        ("20260702", "00"),
    ]


def test_forecast_hour_sequence_validation():
    assert forecast_hour_sequence(12, 3) == [0, 3, 6, 9, 12]
    with pytest.raises(ValidationError, match="evenly divisible"):
        forecast_hour_sequence(10, 3)


def test_html_error_response_rejected():
    with pytest.raises(ValidationError, match="HTML/text"):
        generate_gfs_weather_grib(
            GFSWeatherRequest(
                bbox=BoundingBox(-1, 50, 0, 51),
                output=Path("/tmp/unused.grb2"),
                hours=0,
                cycle="00",
                date="20260701",
            ),
            http_get=lambda url, timeout: b"<html>not found</html>",
        )


def test_empty_response_rejected():
    with pytest.raises(ValidationError, match="empty response"):
        generate_gfs_weather_grib(
            GFSWeatherRequest(
                bbox=BoundingBox(-1, 50, 0, 51),
                output=Path("/tmp/unused.grb2"),
                hours=0,
                cycle="00",
                date="20260701",
            ),
            http_get=lambda url, timeout: b"",
        )


def test_generate_gfs_appends_grib_segments_atomically(monkeypatch, tmp_path: Path):
    calls = []

    def fake_http_get(url, timeout):
        calls.append(url)
        return _fake_grib2(f"payload-{len(calls)}".encode())

    monkeypatch.setattr(
        "tidal_current_grib_generator.weather.inspect_grib",
        lambda path: {
            "stream_valid": True,
            "message_count": 3,
            "edition_counts": {2: 3},
            "first_valid_time": "20260701T0000",
            "last_valid_time": "20260701T0600",
        },
    )
    output = tmp_path / "gfs.grb2"

    result = generate_gfs_weather_grib(
        GFSWeatherRequest(
            bbox=BoundingBox(-8.5, 50.5, -2.5, 56.5),
            output=output,
            hours=6,
            step_hours=3,
            cycle="00",
            date="20260701",
        ),
        http_get=fake_http_get,
    )

    assert output.exists()
    assert result.message_count == 3
    assert result.inspection["edition_counts"] == {2: 3}
    assert len(calls) == 3
    assert output.read_bytes().count(b"GRIB") == 3


def test_generate_gfs_wave_appends_grib_segments_atomically(monkeypatch, tmp_path: Path):
    calls = []

    def fake_http_get(url, timeout):
        calls.append(url)
        return _fake_grib2(f"wave-{len(calls)}".encode())

    monkeypatch.setattr(
        "tidal_current_grib_generator.weather.inspect_grib",
        lambda path: {
            "stream_valid": True,
            "message_count": 3,
            "edition_counts": {2: 3},
            "short_name_counts": {"swh": 1, "perpw": 1, "dirpw": 1},
        },
    )

    result = generate_gfs_wave_grib(
        GFSWaveRequest(
            bbox=BoundingBox(-8.5, 50.5, -2.5, 56.5),
            output=tmp_path / "waves.grb2",
            hours=6,
            step_hours=3,
            cycle="06",
            date="20260701",
        ),
        http_get=fake_http_get,
    )

    assert result.output.exists()
    assert result.provider == "gfs_wave"
    assert len(calls) == 3
    assert all("filter_gfswave.pl" in call for call in calls)


def test_auto_cycle_falls_back_using_mocked_http(monkeypatch, tmp_path: Path):
    calls = []

    def fake_http_get(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            return b"<html>cycle not ready</html>"
        return _fake_grib2(b"ok")

    monkeypatch.setattr(
        "tidal_current_grib_generator.weather.inspect_grib",
        lambda path: {"stream_valid": True, "message_count": 1, "edition_counts": {2: 1}},
    )

    result = generate_gfs_weather_grib(
        GFSWeatherRequest(
            bbox=BoundingBox(-1, 50, 0, 51),
            output=tmp_path / "gfs.grb2",
            hours=0,
            cycle="auto",
            max_auto_cycles=2,
            retry_delay_seconds=0,
        ),
        http_get=fake_http_get,
        now=datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc),
    )

    assert result.cycle == GFSCycle("20260702", "06")
    assert len(calls) == 2


def test_auto_cycle_checks_all_required_gfs_hours(monkeypatch, tmp_path: Path):
    calls = []

    def fake_http_get(url, timeout):
        calls.append(url)
        if "20260702%2F12" in url and "f004" in url:
            return b"<html>not published</html>"
        return _fake_grib2(b"ok")

    monkeypatch.setattr(
        "tidal_current_grib_generator.weather.inspect_grib",
        lambda path: {"stream_valid": True, "message_count": 1, "edition_counts": {2: 1}},
    )

    result = generate_gfs_weather_grib(
        GFSWeatherRequest(
            bbox=BoundingBox(-1, 50, 0, 51),
            output=tmp_path / "gfs.grb2",
            hours=4,
            step_hours=1,
            cycle="auto",
            max_auto_cycles=2,
            retry_delay_seconds=0,
        ),
        http_get=fake_http_get,
        now=datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc),
    )

    assert result.cycle == GFSCycle("20260702", "06")
    assert any("f004" in call for call in calls)


def test_generate_gfs_dry_run_does_not_call_http(tmp_path: Path):
    result = generate_gfs_weather_grib(
        GFSWeatherRequest(
            bbox=BoundingBox(-1, 50, 0, 51),
            output=tmp_path / "gfs.grb2",
            hours=6,
            step_hours=3,
            cycle="auto",
            dry_run=True,
        ),
        http_get=lambda url, timeout: pytest.fail("dry-run should not download"),
        now=datetime(2026, 7, 2, 13, 30, tzinfo=timezone.utc),
    )

    assert result.cycle == GFSCycle("20260702", "12")
    assert result.message_count == 0
    assert len(result.urls) == 3
    assert not result.output.exists()


def test_generate_ecmwf_uses_official_client_request(monkeypatch, tmp_path: Path):
    calls = []

    class FakeClient:
        def retrieve(self, **kwargs):
            calls.append(kwargs)
            Path(kwargs["target"]).write_bytes(_fake_grib2(b"10u") + _fake_grib2(b"10v"))
            return type("Result", (), {"datetime": datetime(2026, 7, 2, 6, tzinfo=timezone.utc)})()

    monkeypatch.setattr(
        "tidal_current_grib_generator.weather.inspect_grib",
        lambda path: {"stream_valid": True, "message_count": 2, "edition_counts": {2: 2}},
    )

    result = generate_ecmwf_weather_grib(
        ECMWFWeatherRequest(
            bbox=BoundingBox(-8.5, 50.5, -2.5, 56.5),
            output=tmp_path / "ecmwf.grb2",
            hours=6,
            step_hours=3,
            cycle="auto",
        ),
        client_factory=lambda **kwargs: FakeClient(),
    )

    assert result.output.exists()
    assert result.provider == "ecmwf_ifs_open"
    assert result.source == "ECMWF IFS Open Data forecast"
    assert result.cycle.cycle_time == "20260702T0600Z"
    assert result.message_count == 2
    assert calls[0]["type"] == "fc"
    assert calls[0]["step"] == [0, 3, 6]
    assert calls[0]["param"] == ["10u", "10v", "msl", "2t"]
    assert "date" not in calls[0]
    assert result.warnings


def test_generate_ecmwf_explicit_cycle_request(monkeypatch, tmp_path: Path):
    calls = []

    class FakeClient:
        def retrieve(self, **kwargs):
            calls.append(kwargs)
            Path(kwargs["target"]).write_bytes(_fake_grib2(b"ok"))
            return object()

    monkeypatch.setattr(
        "tidal_current_grib_generator.weather.inspect_grib",
        lambda path: {"stream_valid": True, "message_count": 1, "edition_counts": {2: 1}},
    )

    result = generate_ecmwf_weather_grib(
        ECMWFWeatherRequest(
            bbox=BoundingBox(-1, 50, 0, 51),
            output=tmp_path / "ecmwf.grb2",
            hours=0,
            cycle="06",
            date="20260702",
        ),
        client_factory=lambda **kwargs: FakeClient(),
    )

    assert result.cycle.cycle_time == "20260702T0600Z"
    assert calls[0]["date"] == "20260702"
    assert calls[0]["time"] == 6


def test_generate_ecmwf_rejects_html_response(monkeypatch, tmp_path: Path):
    class FakeClient:
        def retrieve(self, **kwargs):
            Path(kwargs["target"]).write_bytes(b"<html>not grib</html>")
            return object()

    with pytest.raises(ValidationError, match="HTML/text"):
        generate_ecmwf_weather_grib(
            ECMWFWeatherRequest(
                bbox=BoundingBox(-1, 50, 0, 51),
                output=tmp_path / "ecmwf.grb2",
                hours=0,
            ),
            client_factory=lambda **kwargs: FakeClient(),
        )


def test_weather_providers_cli(capsys):
    rc = main(["weather-providers"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "gfs: NOAA GFS 0.25 degree global forecast" in out
    assert "ukmo_ukv: Met Office UKV 2 km forecast" in out
    assert "ecmwf_ifs_open: ECMWF IFS Open Data forecast" in out
    assert "source: NOAA NOMADS" in out


def test_generate_ukmo_ukv_rejects_outside_domain(tmp_path: Path):
    with pytest.raises(ValidationError, match="outside the supported UK/Ireland regional domain"):
        generate_ukmo_ukv_weather_grib(
            UKMOUKVWeatherRequest(
                bbox=BoundingBox(-40, 30, -39, 31),
                output=tmp_path / "ukv.grb",
                hours=24,
                step_hours=1,
            )
        )


def test_ukmo_ukv_forecast_hour_sequence_hourly_to_24h():
    assert ukmo_ukv_forecast_hour_sequence(24, 1) == list(range(25))


def test_ukmo_ukv_forecast_hour_sequence_hourly_to_54h():
    assert ukmo_ukv_forecast_hour_sequence(54, 1) == list(range(55))


def test_ukmo_ukv_forecast_hour_sequence_mixed_to_72h():
    assert ukmo_ukv_forecast_hour_sequence(72, 1) == list(range(55)) + [57, 60, 63, 66, 69, 72]


def test_ukmo_ukv_forecast_hour_sequence_mixed_to_120h():
    hours = ukmo_ukv_forecast_hour_sequence(120, 1)
    assert len(hours) == 77
    assert hours[:55] == list(range(55))
    assert hours[55:] == list(range(57, 121, 3))


def test_ukmo_ukv_forecast_hour_sequence_three_hourly_to_120h():
    assert ukmo_ukv_forecast_hour_sequence(120, 3) == list(range(0, 121, 3))


def test_generate_ukmo_ukv_rejects_unsupported_step(tmp_path: Path):
    with pytest.raises(ValidationError, match="--step-hours must be 1 or 3 for UKV"):
        generate_ukmo_ukv_weather_grib(
            UKMOUKVWeatherRequest(
                bbox=BoundingBox(-8.5, 50.5, -2.5, 56.5),
                output=tmp_path / "ukv.grb",
                hours=72,
                step_hours=2,
            )
        )


def test_generate_ukmo_ukv_reports_missing_source_files(tmp_path: Path):
    def fake_get(url: str, timeout_seconds: float) -> bytes:
        return b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/" />"""

    with pytest.raises(ValidationError, match="could not find a complete UKV cycle"):
        generate_ukmo_ukv_weather_grib(
            UKMOUKVWeatherRequest(
                bbox=BoundingBox(-8.5, 50.5, -2.5, 56.5),
                output=tmp_path / "ukv.grb",
                hours=24,
                step_hours=1,
                cycle="00",
                date="20260703",
            ),
            http_get=fake_get,
        )


def test_generate_ukmo_ukv_from_synthetic_projected_netcdf_roundtrip(tmp_path: Path):
    pytest.importorskip("eccodes")
    pytest.importorskip("pyproj")
    files = {
        "pressure_at_mean_sea_level": _projected_ukv_netcdf_bytes(
            tmp_path, "pressure_at_mean_sea_level", standard_name="air_pressure_at_mean_sea_level", units="Pa", long_name="Pressure at mean sea level"
        ),
        "temperature_at_screen_level": _projected_ukv_netcdf_bytes(
            tmp_path, "temperature_at_screen_level", standard_name="air_temperature", units="K", long_name="Temperature at screen level"
        ),
        "wind_speed_at_10m": _projected_ukv_netcdf_bytes(
            tmp_path, "wind_speed_at_10m", standard_name="wind_speed", units="m s-1", long_name="Wind speed at 10m"
        ),
        "wind_direction_at_10m": _projected_ukv_netcdf_bytes(
            tmp_path, "wind_direction_at_10m", standard_name="wind_from_direction", units="degree", long_name="Wind direction at 10m"
        ),
    }
    contents = "\n".join(
        f"""
        <Contents>
          <Key>uk-deterministic-2km/20260703T0000Z/20260703T0000Z-PT0000H00M-{name}.nc</Key>
          <LastModified>2026-07-03T01:00:00.000Z</LastModified>
          <Size>{len(data)}</Size>
        </Contents>
        """
        for name, data in files.items()
    )
    run_xml = f"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{contents}</ListBucketResult>""".encode()

    def fake_get(url: str, timeout_seconds: float) -> bytes:
        if "prefix=uk-deterministic-2km%2F20260703T0000Z%2F" in url:
            return run_xml
        if url.endswith(".nc"):
            for name, data in files.items():
                if name in url:
                    return data
        return b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/" />"""

    output = tmp_path / "ukv.grb2"
    result = generate_ukmo_ukv_weather_grib(
        UKMOUKVWeatherRequest(
            bbox=BoundingBox(-5.8, 53.0, -5.2, 53.5),
            output=output,
            hours=0,
            step_hours=1,
            cycle="00",
            date="20260703",
            overwrite=True,
            weather_grid_spacing_deg=0.1,
        ),
        http_get=fake_get,
    )

    assert result.message_count == 4
    assert result.inspection["stream_valid"] is True
    assert result.inspection["short_name_counts"]["10u"] == 1
    assert result.inspection["short_name_counts"]["10v"] == 1
    verification = verify_ukmo_ukv_grib(
        UKMOUKVVerifyRequest(
            bbox=BoundingBox(-5.8, 53.0, -5.2, 53.5),
            grib=output,
            hours=0,
            step_hours=1,
            cycle="00",
            date="20260703",
            download_directory=tmp_path / "verify-downloads",
            weather_grid_spacing_deg=0.1,
            tolerance=0.1,
        ),
        http_get=fake_get,
    )

    assert verification["passed"] is True
    assert verification["comparisons"]["10u_f000"]["max_abs_error"] < 0.1


def test_discover_ukv_source_parses_unsigned_s3_listing():
    root_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Name>met-office-atmospheric-model-data</Name>
      <Prefix></Prefix>
      <CommonPrefixes><Prefix>uk-deterministic/</Prefix></CommonPrefixes>
      <CommonPrefixes><Prefix>other/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    ukv_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Contents>
        <Key>uk-deterministic/ukv/20260702/0600/10u_f006.nc</Key>
        <LastModified>2026-07-02T09:00:00.000Z</LastModified>
        <Size>123456</Size>
      </Contents>
      <Contents>
        <Key>uk-deterministic/ukv/20260702/0600/t2m_f006.nc</Key>
        <LastModified>2026-07-02T09:01:00.000Z</LastModified>
        <Size>234567</Size>
      </Contents>
    </ListBucketResult>"""
    other_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/" />"""

    def fake_get(url: str, timeout_seconds: float) -> bytes:
        if "prefix=uk-deterministic%2F" in url:
            return ukv_xml
        if "prefix=other%2F" in url:
            return other_xml
        if "prefix=" in url:
            return other_xml
        return root_xml

    result = discover_ukmo_ukv_source(max_keys=100, http_get=fake_get, now=datetime(2026, 7, 3, tzinfo=timezone.utc))

    assert result["anonymous_listing"] is True
    assert result["top_level_prefixes"] == ["uk-deterministic/", "other/"]
    assert "uk-deterministic/" in result["likely_ukv_prefixes"]
    assert result["candidate_files"][0]["key"].endswith("10u_f006.nc")
    assert result["candidate_files"][0]["size"] == 123456


def test_discover_ukv_source_reports_listing_error():
    def fake_get(url: str, timeout_seconds: float) -> bytes:
        raise OSError("network unavailable")

    result = discover_ukmo_ukv_source(max_keys=20, http_get=fake_get)

    assert result["anonymous_listing"] is False
    assert "network unavailable" in result["error"]
    assert result["candidate_files"] == []


def test_inspect_ukv_source_reports_blocker(monkeypatch, capsys):
    def fake_inspect(request: UKMOUKVInspectRequest) -> dict[str, object]:
        return {
            "provider": "ukmo_ukv",
            "source": "Met Office UKV 2 km forecast",
            "status": "blocked",
            "implemented": False,
            "selected_cycle": "20260702T0600Z",
            "source_bucket": "s3://met-office-atmospheric-model-data/",
            "source_region": "eu-west-2",
            "anonymous_listing": True,
            "listing_error": None,
            "top_level_prefixes": ["uk-deterministic/"],
            "likely_ukv_prefixes": ["uk-deterministic/"],
            "available_model_runs": ["20260702T0600Z"],
            "source_paths_or_urls": ["https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/uk-deterministic/ukv/20260702/0600/10u_f006.nc"],
            "requested_forecast_hours": [0, 1, 2, 3, 4, 5, 6],
            "available_forecast_hours": [6],
            "available_near_surface_variables": ["uk-deterministic/ukv/20260702/0600/10u_f006.nc"],
            "coordinate_variables": "(requires sample NetCDF download)",
            "grid_mapping": "(requires sample NetCDF download)",
            "source_grid_shape": None,
            "source_lat_lon_coverage": None,
            "bbox_intersects_domain": True,
            "candidate_variables": {"wind_u": ["uk-deterministic/ukv/20260702/0600/10u_f006.nc"]},
            "candidate_files": [{"key": "uk-deterministic/ukv/20260702/0600/10u_f006.nc", "size": 123456}],
            "blocker": "UKV source discovery can list anonymous S3 objects, but GRIB generation remains disabled.",
        }

    monkeypatch.setattr("tidal_current_grib_generator.cli.inspect_ukmo_ukv_source", fake_inspect)
    rc = main(
        [
            "inspect-ukv-source",
            "--cycle",
            "auto",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--hours",
            "6",
            "--verbose",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "provider: ukmo_ukv" in out
    assert "status: blocked" in out
    assert "source_bucket: s3://met-office-atmospheric-model-data/" in out
    assert "anonymous_listing: True" in out
    assert "top_level_prefixes: ['uk-deterministic/']" in out
    assert "candidate_variables:" in out
    assert "GRIB generation remains disabled" in out


def test_discover_ukv_source_cli(monkeypatch, capsys):
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.discover_ukmo_ukv_source",
        lambda max_keys: {
            "bucket": "met-office-atmospheric-model-data",
            "region": "eu-west-2",
            "anonymous_listing": True,
            "top_level_prefixes": ["uk-deterministic/"],
            "likely_ukv_prefixes": ["uk-deterministic/"],
            "candidate_files": [{"key": "uk-deterministic/ukv/20260702/0600/10u_f006.nc", "size": 123456}],
            "object_count_seen": 1,
            "error": None,
        },
    )

    rc = main(["discover-ukv-source", "--max-keys", "20"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "bucket: s3://met-office-atmospheric-model-data/" in out
    assert "anonymous_listing: True" in out
    assert "uk-deterministic/ukv/20260702/0600/10u_f006.nc (123456 bytes)" in out


def _tiny_ukv_netcdf_bytes(tmp_path: Path, variable_name: str, *, standard_name: str, units: str, long_name: str) -> bytes:
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    path = tmp_path / f"{variable_name}.nc"
    data = np.arange(12, dtype=float).reshape(3, 4)
    ds = xr.Dataset(
        data_vars={
            variable_name: (
                ("projection_y_coordinate", "projection_x_coordinate"),
                data,
                {
                    "standard_name": standard_name,
                    "units": units,
                    "long_name": long_name,
                    "grid_mapping": "transverse_mercator",
                },
            ),
            "transverse_mercator": (
                (),
                0,
                {
                    "grid_mapping_name": "transverse_mercator",
                    "longitude_of_central_meridian": -2.0,
                    "latitude_of_projection_origin": 49.0,
                    "false_easting": 400000.0,
                    "false_northing": -100000.0,
                },
            ),
        },
        coords={
            "projection_x_coordinate": (
                "projection_x_coordinate",
                [0.0, 2000.0, 4000.0, 6000.0],
                {"standard_name": "projection_x_coordinate", "units": "m"},
            ),
            "projection_y_coordinate": (
                "projection_y_coordinate",
                [0.0, 2000.0, 4000.0],
                {"standard_name": "projection_y_coordinate", "units": "m"},
            ),
            "latitude": (
                ("projection_y_coordinate", "projection_x_coordinate"),
                np.array([[50.0, 50.0, 50.0, 50.0], [51.0, 51.0, 51.0, 51.0], [52.0, 52.0, 52.0, 52.0]]),
            ),
            "longitude": (
                ("projection_y_coordinate", "projection_x_coordinate"),
                np.array([[-6.0, -5.0, -4.0, -3.0], [-6.0, -5.0, -4.0, -3.0], [-6.0, -5.0, -4.0, -3.0]]),
            ),
            "forecast_period": ((), np.timedelta64(0, "h"), {"standard_name": "forecast_period"}),
            "forecast_reference_time": ((), np.datetime64("2026-07-03T00:00:00")),
            "time": ((), np.datetime64("2026-07-03T00:00:00")),
        },
    )
    ds.to_netcdf(path)
    return path.read_bytes()


def _projected_ukv_netcdf_bytes(tmp_path: Path, variable_name: str, *, standard_name: str, units: str, long_name: str) -> bytes:
    xr = pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    pyproj = pytest.importorskip("pyproj")
    path = tmp_path / f"projected_{variable_name}.nc"
    grid_mapping_attrs = {
        "grid_mapping_name": "lambert_azimuthal_equal_area",
        "latitude_of_projection_origin": 54.9,
        "longitude_of_projection_origin": -2.5,
        "false_easting": 0.0,
        "false_northing": 0.0,
        "semi_major_axis": 6378137.0,
        "inverse_flattening": 298.257223563,
    }
    transformer = pyproj.Transformer.from_crs(pyproj.CRS.from_epsg(4326), pyproj.CRS.from_cf(grid_mapping_attrs), always_xy=True)
    x0, y0 = transformer.transform(-5.9, 52.9)
    x1, y1 = transformer.transform(-5.1, 53.6)
    x = np.linspace(min(x0, x1) - 20_000.0, max(x0, x1) + 20_000.0, 8)
    y = np.linspace(min(y0, y1) - 20_000.0, max(y0, y1) + 20_000.0, 7)
    xx, yy = np.meshgrid(x, y)
    if variable_name == "pressure_at_mean_sea_level":
        data = 101500.0 + 1.0e-4 * xx + 2.0e-4 * yy
    elif variable_name == "temperature_at_screen_level":
        data = 285.0 + 1.0e-5 * xx - 1.0e-5 * yy
    elif variable_name == "wind_speed_at_10m":
        data = np.full_like(xx, 10.0)
    elif variable_name == "wind_direction_at_10m":
        data = np.full_like(xx, 270.0)
    else:
        data = xx * 0.0
    ds = xr.Dataset(
        data_vars={
            variable_name: (
                ("projection_y_coordinate", "projection_x_coordinate"),
                data,
                {
                    "standard_name": standard_name,
                    "units": units,
                    "long_name": long_name,
                    "grid_mapping": "lambert_azimuthal_equal_area",
                },
            ),
            "lambert_azimuthal_equal_area": ((), 0, grid_mapping_attrs),
        },
        coords={
            "projection_x_coordinate": ("projection_x_coordinate", x, {"standard_name": "projection_x_coordinate", "units": "m"}),
            "projection_y_coordinate": ("projection_y_coordinate", y, {"standard_name": "projection_y_coordinate", "units": "m"}),
            "forecast_period": ((), np.timedelta64(0, "h"), {"standard_name": "forecast_period"}),
            "forecast_reference_time": ((), np.datetime64("2026-07-03T00:00:00")),
            "time": ((), np.datetime64("2026-07-03T00:00:00")),
        },
    )
    ds.to_netcdf(path)
    return path.read_bytes()


def test_inspect_ukv_netcdf_with_mocked_downloads(tmp_path: Path):
    files = {
        "pressure_at_mean_sea_level": _tiny_ukv_netcdf_bytes(
            tmp_path, "pressure_at_mean_sea_level", standard_name="air_pressure_at_mean_sea_level", units="Pa", long_name="Pressure at mean sea level"
        ),
        "temperature_at_screen_level": _tiny_ukv_netcdf_bytes(
            tmp_path, "temperature_at_screen_level", standard_name="air_temperature", units="K", long_name="Temperature at screen level"
        ),
        "wind_speed_at_10m": _tiny_ukv_netcdf_bytes(
            tmp_path, "wind_speed_at_10m", standard_name="wind_speed", units="m s-1", long_name="Wind speed at 10m"
        ),
        "wind_direction_at_10m": _tiny_ukv_netcdf_bytes(
            tmp_path, "wind_direction_at_10m", standard_name="wind_from_direction", units="degree", long_name="Wind direction at 10m"
        ),
    }
    contents = "\n".join(
        f"""
        <Contents>
          <Key>uk-deterministic-2km/20260703T0000Z/20260703T0000Z-PT0000H00M-{name}.nc</Key>
          <LastModified>2026-07-03T01:00:00.000Z</LastModified>
          <Size>{len(data)}</Size>
        </Contents>
        """
        for name, data in files.items()
    )
    root_xml = b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <CommonPrefixes><Prefix>uk-deterministic-2km/</Prefix></CommonPrefixes>
    </ListBucketResult>"""
    run_xml = f"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{contents}</ListBucketResult>""".encode()

    def fake_get(url: str, timeout_seconds: float) -> bytes:
        if "prefix=uk-deterministic-2km%2F20260703T0000Z%2F" in url:
            return run_xml
        if url.endswith(".nc"):
            for name, data in files.items():
                if name in url:
                    return data
        if "prefix=" in url:
            return b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/" />"""
        return root_xml

    result = inspect_ukmo_ukv_netcdf(
        UKMOUKVNetCDFInspectRequest(
            bbox=BoundingBox(-5.8, 50.5, -3.5, 51.5),
            hours=0,
            download_directory=tmp_path / "downloads",
            max_keys=80,
            extract_sample=True,
        ),
        http_get=fake_get,
    )

    assert result["selected_cycle"] == "20260703T0000Z"
    assert result["generation_enabled"] is False
    assert result["files"]["pressure_msl_h000"]["primary_data_variable"] == "pressure_at_mean_sea_level"
    assert result["coordinate_summary"]["grid_type"] == "projected_or_curvilinear_with_auxiliary_2d_lat_lon"
    assert result["time_summary"]["requested_hours_available"] is True
    assert result["time_summary"]["hourly_0_to_54_proven"] is False
    assert result["wind_direction_convention"]["status"] == "usable"
    assert result["wind_uv_sample_stats"]["convention"] == "meteorological_from"
    assert result["variable_mappings"]["pressure_msl_h000"]["field"] == "pressure_msl"
    assert result["variable_mappings"]["pressure_msl_h000"]["unit_conversion_required"] == "none if writing GRIB pressure in Pa"


def test_inspect_ukv_netcdf_regrid_sample_from_projected_grid(tmp_path: Path):
    pytest.importorskip("pyproj")
    files = {
        "pressure_at_mean_sea_level": _projected_ukv_netcdf_bytes(
            tmp_path, "pressure_at_mean_sea_level", standard_name="air_pressure_at_mean_sea_level", units="Pa", long_name="Pressure at mean sea level"
        ),
        "temperature_at_screen_level": _projected_ukv_netcdf_bytes(
            tmp_path, "temperature_at_screen_level", standard_name="air_temperature", units="K", long_name="Temperature at screen level"
        ),
        "wind_speed_at_10m": _projected_ukv_netcdf_bytes(
            tmp_path, "wind_speed_at_10m", standard_name="wind_speed", units="m s-1", long_name="Wind speed at 10m"
        ),
        "wind_direction_at_10m": _projected_ukv_netcdf_bytes(
            tmp_path, "wind_direction_at_10m", standard_name="wind_from_direction", units="degree", long_name="Wind direction at 10m"
        ),
    }
    contents = "\n".join(
        f"""
        <Contents>
          <Key>uk-deterministic-2km/20260703T0000Z/20260703T0000Z-PT0000H00M-{name}.nc</Key>
          <LastModified>2026-07-03T01:00:00.000Z</LastModified>
          <Size>{len(data)}</Size>
        </Contents>
        """
        for name, data in files.items()
    )
    run_xml = f"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{contents}</ListBucketResult>""".encode()

    def fake_get(url: str, timeout_seconds: float) -> bytes:
        if "prefix=uk-deterministic-2km%2F20260703T0000Z%2F" in url:
            return run_xml
        if url.endswith(".nc"):
            for name, data in files.items():
                if name in url:
                    return data
        return b"""<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/" />"""

    result = inspect_ukmo_ukv_netcdf(
        UKMOUKVNetCDFInspectRequest(
            bbox=BoundingBox(-5.8, 53.0, -5.2, 53.5),
            hours=0,
            cycle="00",
            date="20260703",
            download_directory=tmp_path / "downloads",
            extract_sample=True,
            weather_grid_spacing_deg=0.1,
        ),
        http_get=fake_get,
    )

    regrid = result["regrid_sample"]
    assert regrid["status"] == "ok"
    assert regrid["output_grid"]["nx"] == 7
    assert regrid["output_grid"]["ny"] == 6
    assert regrid["fields"]["pressure_msl"]["missing_percent"] == 0.0
    assert regrid["fields"]["wind_u_10m_candidate"]["missing_percent"] == 0.0
    assert regrid["fields"]["wind_v_10m_candidate"]["missing_percent"] == 0.0
    assert regrid["fields"]["wind_u_10m_candidate"]["stats"]["mean"] == pytest.approx(10.0)
    assert regrid["fields"]["wind_v_10m_candidate"]["stats"]["mean"] == pytest.approx(0.0, abs=1e-12)


def test_inspect_ukv_netcdf_cli(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.inspect_ukmo_ukv_netcdf",
        lambda request: {
            "provider": "ukmo_ukv",
            "source": "Met Office UKV 2 km forecast",
            "status": "metadata-only",
            "implemented": False,
            "selected_cycle": "20260703T0000Z",
            "download_directory": str(tmp_path),
            "downloaded_files": {"pressure_msl": {"path": str(tmp_path / "p.nc"), "size": 10, "reused": False}},
            "files": {},
            "coordinate_summary": {"grid_type": "projected_xy_with_cf_grid_mapping"},
            "time_summary": {"requested_forecast_hours": [0]},
            "variable_mappings": {},
            "wind_direction_convention": {"status": "ambiguous"},
            "crop_feasibility": {},
            "generation_enabled": False,
            "blocker": "disabled",
        },
    )

    rc = main(
        [
            "inspect-ukv-netcdf",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--hours",
            "0",
            "--download-directory",
            str(tmp_path),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "status: metadata-only" in out
    assert "generation_enabled: False" in out


def test_wind_speed_direction_to_uv_from_convention():
    np = pytest.importorskip("numpy")
    speed = np.array([10.0, 10.0, 10.0, 10.0])
    direction = np.array([0.0, 90.0, 180.0, 270.0])
    u, v = wind_speed_direction_to_uv(speed, direction)

    assert np.allclose(u, [0.0, -10.0, 0.0, 10.0], atol=1e-12)
    assert np.allclose(v, [-10.0, 0.0, 10.0, 0.0], atol=1e-12)


def test_inspect_ukv_source_rejects_outside_domain(capsys):
    rc = main(
        [
            "inspect-ukv-source",
            "--bbox",
            "-40",
            "30",
            "-39",
            "31",
            "--hours",
            "6",
        ]
    )

    assert rc == 2
    assert "outside the supported UK/Ireland regional domain" in capsys.readouterr().err


def test_generate_weather_cli_metadata(monkeypatch, tmp_path: Path, capsys):
    class FakeCycle:
        cycle_time = "20260701T0000Z"

    class FakeResult:
        provider = "gfs"
        source = "NOAA GFS 0.25° forecast via NOMADS"
        model = "gfs_0p25"
        cycle = FakeCycle()
        bbox = BoundingBox(-1, 50, 0, 51)
        forecast_hours = [0, 3]
        output = tmp_path / "gfs.grb2"
        byte_count = 40
        message_count = 2
        inspection = {"first_valid_time": "20260701T0000", "last_valid_time": "20260701T0300"}
        urls = []

        def as_dict(self):
            return {"provider": self.provider}

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_gfs_weather_grib", lambda request, progress_callback=None: FakeResult())

    rc = main(
        [
            "generate-weather",
            "--provider",
            "gfs",
            "--bbox",
            "-1",
            "50",
            "0",
            "51",
            "--cycle",
            "00",
            "--date",
            "20260701",
            "--hours",
            "3",
            "--step-hours",
            "3",
            "--output",
            str(tmp_path / "gfs.grb2"),
            "--metadata-summary",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Source: NOAA GFS 0.25° forecast via NOMADS" in out
    assert "validated GRIB stream: 2 messages, 40 bytes" in out


def test_generate_weather_cli_ecmwf_metadata(monkeypatch, tmp_path: Path, capsys):
    class FakeCycle:
        cycle_time = "20260702T0600Z"

    class FakeResult:
        provider = "ecmwf_ifs_open"
        source = "ECMWF IFS Open Data forecast"
        model = "ecmwf_ifs_open_0p25"
        cycle = FakeCycle()
        bbox = BoundingBox(-1, 50, 0, 51)
        forecast_hours = [0, 3]
        output = tmp_path / "ecmwf.grb2"
        byte_count = 40
        message_count = 2
        inspection = {"first_valid_time": "20260702T0600", "last_valid_time": "20260702T0900"}
        urls = []
        warnings = ["bbox not cropped"]

        def as_dict(self):
            return {"provider": self.provider}

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_ecmwf_weather_grib", lambda request, progress_callback=None: FakeResult())

    rc = main(
        [
            "generate-weather",
            "--provider",
            "ecmwf_ifs_open",
            "--bbox",
            "-1",
            "50",
            "0",
            "51",
            "--cycle",
            "auto",
            "--hours",
            "3",
            "--step-hours",
            "3",
            "--output",
            str(tmp_path / "ecmwf.grb2"),
            "--metadata-summary",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Source: ECMWF IFS Open Data forecast" in out
    assert "validated GRIB stream: 2 messages, 40 bytes" in out


def test_generate_weather_cli_dwd_reports_not_implemented(capsys, tmp_path: Path):
    rc = main(
        [
            "generate-weather",
            "--provider",
            "dwd_icon_eu",
            "--bbox",
            "-1",
            "50",
            "0",
            "51",
            "--cycle",
            "auto",
            "--hours",
            "3",
            "--step-hours",
            "3",
            "--output",
            str(tmp_path / "dwd.grb2"),
        ]
    )

    assert rc == 2
    assert "DWD ICON-EU provider is not implemented yet" in capsys.readouterr().err


def test_generate_weather_cli_ukmo_ukv_metadata(monkeypatch, capsys, tmp_path: Path):
    class FakeCycle:
        cycle_time = "20260703T0000Z"

    class FakeResult:
        provider = "ukmo_ukv"
        source = "Met Office UKV 2 km forecast"
        model = "uk_deterministic_2km"
        cycle = FakeCycle()
        bbox = BoundingBox(-8.5, 50.5, -2.5, 56.5)
        forecast_hours = [0]
        output = tmp_path / "ukv.grb"
        byte_count = 20
        message_count = 4
        inspection = {"stream_valid": True, "message_count": 4, "edition_counts": {2: 4}}
        urls = []
        warnings = []

        def as_dict(self):
            return {"provider": self.provider}

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_ukmo_ukv_weather_grib", lambda request, progress_callback=None: FakeResult())
    rc = main(
        [
            "generate-weather",
            "--provider",
            "ukmo_ukv",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--cycle",
            "auto",
            "--hours",
            "24",
            "--step-hours",
            "1",
            "--weather-preset",
            "routing",
            "--metadata-summary",
            "--output",
            str(tmp_path / "ukv.grb"),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "Source: Met Office UKV 2 km forecast" in out
    assert "validated GRIB stream: 4 messages" in out


def test_merge_gribs_current_first(monkeypatch, tmp_path: Path):
    current = tmp_path / "current.grb"
    weather = tmp_path / "weather.grb2"
    output = tmp_path / "merged.grb"
    current_bytes = _fake_grib1(b"current-u") + _fake_grib1(b"current-v")
    weather_bytes = _fake_grib2(b"weather-u") + _fake_grib2(b"weather-t")
    current.write_bytes(current_bytes)
    weather.write_bytes(weather_bytes)
    monkeypatch.setattr(
        "tidal_current_grib_generator.grib.merge.inspect_grib",
        lambda path: {"stream_valid": True, "message_count": 4, "edition_counts": {1: 2, 2: 2}},
    )

    result = merge_grib_files(current, weather, output)

    assert result.current_message_count == 2
    assert result.weather_message_count == 2
    assert result.output_message_count == 4
    assert output.read_bytes() == current_bytes + weather_bytes


def test_merge_gribs_cli(monkeypatch, tmp_path: Path, capsys):
    current = tmp_path / "current.grb"
    weather = tmp_path / "weather.grb2"
    output = tmp_path / "merged.grb"
    current.write_bytes(_fake_grib1(b"current"))
    weather.write_bytes(_fake_grib2(b"weather"))
    monkeypatch.setattr(
        "tidal_current_grib_generator.grib.merge.inspect_grib",
        lambda path: {"stream_valid": True, "message_count": 2, "edition_counts": {1: 1, 2: 1}},
    )

    rc = main(
        [
            "merge-gribs",
            "--current",
            str(current),
            "--weather",
            str(weather),
            "--output",
            str(output),
            "--metadata-summary",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "order: current first, weather second" in out
    assert "output messages: 2" in out
    assert output.exists()


def test_generate_environment_grib_existing_current_and_weather(monkeypatch, tmp_path: Path, capsys):
    current = tmp_path / "current.grb"
    weather = tmp_path / "weather.grb2"
    output = tmp_path / "environment.grb"
    current.write_bytes(_fake_grib1(b"current"))
    weather.write_bytes(_fake_grib2(b"weather"))
    monkeypatch.setattr(
        "tidal_current_grib_generator.grib.merge.inspect_grib",
        lambda path: {
            "stream_valid": True,
            "message_count": 2,
            "edition_counts": {1: 1, 2: 1},
            "short_name_counts": {"unknown": 1, "10u": 1},
            "current_component_counts": {"u_49": 1, "v_50": 0},
        },
    )

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "existing-file",
            "--weather-file",
            str(weather),
            "--current-source",
            "existing-file",
            "--current-file",
            str(current),
            "--hours",
            "3",
            "--output",
            str(output),
            "--metadata-summary",
        ]
    )

    assert rc == 0
    assert output.read_bytes() == current.read_bytes() + weather.read_bytes()
    out = capsys.readouterr().out
    assert "current messages: 1" in out
    assert "weather messages: 1" in out


def test_generate_environment_grib_weather_only(monkeypatch, tmp_path: Path):
    weather = tmp_path / "weather.grb2"
    output = tmp_path / "environment.grb"
    weather.write_bytes(_fake_grib2(b"weather"))
    monkeypatch.setattr(
        "tidal_current_grib_generator.grib.merge.inspect_grib",
        lambda path: {"stream_valid": True, "message_count": 1, "edition_counts": {2: 1}},
    )

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "existing-file",
            "--weather-file",
            str(weather),
            "--current-source",
            "none",
            "--hours",
            "3",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert output.read_bytes() == weather.read_bytes()


def test_generate_environment_grib_include_waves_requires_gfs(tmp_path: Path, capsys):
    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "ecmwf_ifs_open",
            "--include-waves",
            "--current-source",
            "none",
            "--hours",
            "3",
            "--output",
            str(tmp_path / "out.grb"),
        ]
    )

    assert rc == 2
    assert "--include-waves is currently supported only with --weather-provider gfs" in capsys.readouterr().err


def test_generate_environment_grib_ukmo_ukv_weather_only_mocked(monkeypatch, tmp_path: Path):
    def fake_weather(request, progress_callback=None):
        request.output.write_bytes(_fake_grib2(b"ukv"))
        class FakeCycle:
            cycle_time = "20260703T0000Z"

        class FakeResult:
            cycle = FakeCycle()
            warnings = []

        return FakeResult()

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_ukmo_ukv_weather_grib", fake_weather)
    monkeypatch.setattr(
        "tidal_current_grib_generator.grib.merge.inspect_grib",
        lambda path: {"stream_valid": True, "message_count": 1, "edition_counts": {2: 1}},
    )
    output = tmp_path / "environment.grb"
    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "ukmo_ukv",
            "--current-source",
            "none",
            "--hours",
            "0",
            "--step-hours",
            "1",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert output.read_bytes() == _fake_grib2(b"ukv")


def _fake_weather_result(request, *, provider: str, source: str, model: str, cycle: GFSCycle | None = None) -> WeatherGenerateResult:
    cycle = cycle or GFSCycle("20260702", "06")
    request.output.write_bytes(_fake_grib2(provider.encode()))
    return WeatherGenerateResult(
        provider=provider,
        source=source,
        model=model,
        cycle=cycle,
        bbox=request.bbox,
        forecast_hours=forecast_hour_sequence(request.hours, request.step_hours),
        output=request.output,
        byte_count=request.output.stat().st_size,
        message_count=1,
        inspection={"stream_valid": True, "message_count": 1},
        urls=[],
        variables_levels={},
    )


def _poison_ukv_hour_helper(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("UKV forecast-hour helper must not be called for this provider")

    monkeypatch.setattr("tidal_current_grib_generator.cli.ukmo_ukv_forecast_hour_sequence", fail)
    monkeypatch.setattr("tidal_current_grib_generator.weather.ukmo_ukv_forecast_hour_sequence", fail)


def test_generate_environment_grib_gfs_copernicus_does_not_call_ukv_helper(monkeypatch, tmp_path: Path):
    _poison_ukv_hour_helper(monkeypatch)
    calls = {"weather": [], "current": []}

    def fake_weather(request, progress_callback=None):
        calls["weather"].append(request)
        return _fake_weather_result(request, provider="gfs", source="NOAA GFS 0.25° forecast via NOMADS", model="gfs_0p25")

    def fake_current(args, *, current_source, bbox, start, output, temp_dir):
        calls["current"].append(current_source)
        output.write_bytes(_fake_grib1(b"copernicus-current"))

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_gfs_weather_grib", fake_weather)
    monkeypatch.setattr("tidal_current_grib_generator.cli._generate_environment_current_source", fake_current)

    output = tmp_path / "environment.grb"
    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-6.5",
            "52.8",
            "-4.5",
            "54.5",
            "--cycle",
            "auto",
            "--hours",
            "6",
            "--step-hours",
            "1",
            "--weather-provider",
            "gfs",
            "--weather-preset",
            "routing",
            "--current-source",
            "copernicus_nws",
            "--username",
            "user@example.invalid",
            "--output",
            str(output),
            "--metadata-summary",
        ]
    )

    assert rc == 0
    assert calls["weather"][0].step_hours == 1
    assert calls["current"] == ["copernicus_nws"]
    assert output.exists()


def test_generate_environment_grib_gfs_tpxo_cache_does_not_call_ukv_helper(monkeypatch, tmp_path: Path):
    _poison_ukv_hour_helper(monkeypatch)
    cache = tmp_path / "cache.tpxocache"
    cache.write_bytes(b"cache")
    calls = _patch_generated_current(monkeypatch)

    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.validate_tpxo_cache",
        lambda path: {
            "bbox": {"west": -6.5, "south": 52.8, "east": -4.5, "north": 54.5},
            "grid_spacing_deg": 0.05,
            "model_name": "TPXO10-atlas-v2-nc",
            "stale": False,
        },
    )
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.generate_gfs_weather_grib",
        lambda request, progress_callback=None: _fake_weather_result(
            request, provider="gfs", source="NOAA GFS 0.25° forecast via NOMADS", model="gfs_0p25"
        ),
    )

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-6.5",
            "52.8",
            "-4.5",
            "54.5",
            "--cycle",
            "auto",
            "--hours",
            "6",
            "--step-hours",
            "1",
            "--weather-provider",
            "gfs",
            "--current-source",
            "tpxo-cache",
            "--input-cache",
            str(cache),
            "--grid-spacing-deg",
            "0.05",
            "--output",
            str(tmp_path / "environment.grb"),
        ]
    )

    assert rc == 0
    assert calls[0].source == "tpxo-cache"


def test_generate_environment_grib_ecmwf_existing_current_does_not_call_ukv_helper(monkeypatch, tmp_path: Path):
    _poison_ukv_hour_helper(monkeypatch)
    current = tmp_path / "current.grb"
    current.write_bytes(_fake_grib1(b"current"))

    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.generate_ecmwf_weather_grib",
        lambda request, progress_callback=None: _fake_weather_result(
            request,
            provider="ecmwf_ifs_open",
            source="ECMWF IFS Open Data forecast",
            model="ecmwf_ifs_open_0p25",
            cycle=GFSCycle("20260702", "06"),
        ),
    )

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-6.5",
            "52.8",
            "-4.5",
            "54.5",
            "--date",
            "20260702",
            "--cycle",
            "06",
            "--hours",
            "6",
            "--step-hours",
            "3",
            "--weather-provider",
            "ecmwf_ifs_open",
            "--current-source",
            "existing-file",
            "--current-file",
            str(current),
            "--output",
            str(tmp_path / "environment.grb"),
        ]
    )

    assert rc == 0


def test_generate_environment_grib_ukv_uses_mixed_cadence_helper(monkeypatch, tmp_path: Path):
    calls = {"helper": []}
    cache = tmp_path / "cache.tpxocache"
    cache.write_bytes(b"cache")
    current_calls = _patch_generated_current(monkeypatch)

    def fake_helper(hours: int, step_hours: int) -> list[int]:
        calls["helper"].append((hours, step_hours))
        return [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15]

    monkeypatch.setattr("tidal_current_grib_generator.cli.ukmo_ukv_forecast_hour_sequence", fake_helper)
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.validate_tpxo_cache",
        lambda path: {
            "bbox": {"west": -6.5, "south": 52.8, "east": -4.5, "north": 54.5},
            "grid_spacing_deg": 0.05,
            "model_name": "TPXO10-atlas-v2-nc",
            "stale": False,
        },
    )
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.generate_ukmo_ukv_weather_grib",
        lambda request, progress_callback=None: _fake_weather_result(
            request, provider="ukmo_ukv", source="Met Office UKV 2 km forecast", model="uk_deterministic_2km"
        ),
    )

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-6.5",
            "52.8",
            "-4.5",
            "54.5",
            "--cycle",
            "auto",
            "--hours",
            "72",
            "--step-hours",
            "1",
            "--weather-provider",
            "ukmo_ukv",
            "--current-source",
            "tpxo-cache",
            "--input-cache",
            str(cache),
            "--grid-spacing-deg",
            "0.05",
            "--output",
            str(tmp_path / "environment.grb"),
            "--metadata-summary",
        ]
    )

    assert rc == 0
    assert calls["helper"] == [(72, 1)]
    assert current_calls[0].source == "tpxo-cache"


def test_generate_environment_grib_hourly_weather_with_three_hour_waves(monkeypatch, tmp_path: Path, capsys):
    calls = {"weather": [], "waves": []}

    def fake_weather(request, progress_callback=None):
        calls["weather"].append(request)
        request.output.write_bytes(_fake_grib2(b"weather"))
        return WeatherGenerateResult(
            provider="gfs",
            source="NOAA GFS 0.25° forecast via NOMADS",
            model="gfs_0p25",
            cycle=GFSCycle("20260702", "06"),
            bbox=request.bbox,
            forecast_hours=forecast_hour_sequence(request.hours, request.step_hours),
            output=request.output,
            byte_count=request.output.stat().st_size,
            message_count=1,
            inspection={"stream_valid": True, "message_count": 1},
            urls=[],
            variables_levels={},
        )

    def fake_wave(request, progress_callback=None):
        calls["waves"].append(request)
        request.output.write_bytes(_fake_grib2(b"wave"))
        return WeatherGenerateResult(
            provider="gfs_wave",
            source="NOAA GFS Wave forecast via NOMADS",
            model="gfswave_global_0p25",
            cycle=GFSCycle("20260702", "06"),
            bbox=request.bbox,
            forecast_hours=forecast_hour_sequence(request.hours, request.step_hours),
            output=request.output,
            byte_count=request.output.stat().st_size,
            message_count=1,
            inspection={"stream_valid": True, "message_count": 1},
            urls=[],
            variables_levels={},
        )

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_gfs_weather_grib", fake_weather)
    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_gfs_wave_grib", fake_wave)

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "gfs",
            "--include-waves",
            "--step-hours",
            "1",
            "--hours",
            "6",
            "--current-source",
            "none",
            "--output",
            str(tmp_path / "environment.grb"),
            "--metadata-summary",
        ]
    )

    assert rc == 0
    assert calls["weather"][0].step_hours == 1
    assert calls["waves"][0].step_hours == 3
    assert "Wave fields will be included every 3 hours; weather/current fields remain every 1 hour." in capsys.readouterr().out


def _patch_generated_current(monkeypatch):
    calls = []

    def fake_cmd_generate(args):
        calls.append(args)
        args.output.write_bytes(_fake_grib1(f"current-{args.source}".encode()))
        return 0

    monkeypatch.setattr("tidal_current_grib_generator.cli.cmd_generate", fake_cmd_generate)
    return calls


def test_generate_environment_grib_tpxo_cache_current(monkeypatch, tmp_path: Path):
    calls = _patch_generated_current(monkeypatch)
    cache = tmp_path / "cache.tpxocache"
    cache.write_bytes(b"cache")
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.validate_tpxo_cache",
        lambda path: {
            "bbox": {"west": -8.5, "south": 50.5, "east": -2.5, "north": 56.5},
            "grid_spacing_deg": 0.05,
            "model_name": "TPXO10-atlas-v2-nc",
            "stale": False,
        },
    )
    output = tmp_path / "environment.grb"

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "none",
            "--current-source",
            "tpxo-cache",
            "--input-cache",
            str(cache),
            "--start",
            "2026-07-02T00:00:00Z",
            "--hours",
            "3",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert calls[0].source == "tpxo-cache"
    assert calls[0].input_cache == cache
    assert output.read_bytes().startswith(b"GRIB")


def test_generate_environment_grib_tpxo_cache_missing_without_auto_prepare(tmp_path: Path, capsys):
    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "none",
            "--current-source",
            "tpxo-cache",
            "--input-cache",
            str(tmp_path / "missing.tpxocache"),
            "--start",
            "2026-07-02T00:00:00Z",
            "--hours",
            "3",
            "--output",
            str(tmp_path / "environment.grb"),
        ]
    )

    assert rc == 2
    assert "--auto-prepare-tpxo-cache" in capsys.readouterr().err


def test_generate_environment_grib_tpxo_cache_auto_prepare(monkeypatch, tmp_path: Path):
    calls = _patch_generated_current(monkeypatch)
    prepared = {}

    def fake_prepare(**kwargs):
        prepared.update(kwargs)
        kwargs["output"].write_bytes(b"prepared")

        class FakePrepared:
            preparation_seconds = 0.5

        return FakePrepared()

    monkeypatch.setattr("tidal_current_grib_generator.cli.prepare_tpxo_cache", fake_prepare)
    output = tmp_path / "environment.grb"
    cache = tmp_path / "new.tpxocache"

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "none",
            "--current-source",
            "tpxo-cache",
            "--input-cache",
            str(cache),
            "--auto-prepare-tpxo-cache",
            "--model-dir",
            str(tmp_path),
            "--model-name",
            "TPXO10-atlas-v2-nc",
            "--grid-spacing-deg",
            "0.05",
            "--start",
            "2026-07-02T00:00:00Z",
            "--hours",
            "3",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert prepared["output"] == cache
    assert calls[0].input_cache == cache


def test_generate_environment_grib_tpxo_direct_current(monkeypatch, tmp_path: Path):
    calls = _patch_generated_current(monkeypatch)
    output = tmp_path / "environment.grb"

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-6.0",
            "53.0",
            "-5.5",
            "53.5",
            "--weather-provider",
            "none",
            "--current-source",
            "tpxo",
            "--model-dir",
            str(tmp_path),
            "--model-name",
            "TPXO10-atlas-v2-nc",
            "--start",
            "2026-07-02T00:00:00Z",
            "--hours",
            "3",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert calls[0].source == "tpxo"
    assert calls[0].model_directory == tmp_path


def test_generate_environment_grib_marine_ie_current(monkeypatch, tmp_path: Path):
    calls = []

    def fake_download(output, overwrite=False, progress_callback=None):
        calls.append((output, overwrite))
        output.write_bytes(_fake_grib1(b"marine"))
        return object()

    monkeypatch.setattr("tidal_current_grib_generator.cli.download_marine_ie_irish_sea_grib", fake_download)
    output = tmp_path / "environment.grb"

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-6.0",
            "53.0",
            "-5.5",
            "53.5",
            "--weather-provider",
            "none",
            "--current-source",
            "marine_ie_irish_sea",
            "--hours",
            "24",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert calls and calls[0][1] is True


def test_generate_environment_grib_copernicus_current(monkeypatch, tmp_path: Path):
    calls = []

    def fake_copernicus(args):
        calls.append(args)
        args.output.write_bytes(_fake_grib1(b"copernicus"))
        return 0

    monkeypatch.setattr("tidal_current_grib_generator.cli.cmd_generate_copernicus", fake_copernicus)
    output = tmp_path / "environment.grb"

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "none",
            "--current-source",
            "copernicus_nws",
            "--username",
            "user@example.com",
            "--password-env",
            "CURRENTGRIB_COPERNICUS_PASSWORD",
            "--start",
            "2026-07-02T00:00:00Z",
            "--hours",
            "3",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert calls[0].provider == "copernicus_nws"
    assert calls[0].username == "user@example.com"
    assert calls[0].password_env == "CURRENTGRIB_COPERNICUS_PASSWORD"


def test_generate_environment_grib_auto_current_selects_marine(monkeypatch, tmp_path: Path, capsys):
    def fake_download(output, overwrite=False, progress_callback=None):
        output.write_bytes(_fake_grib1(b"marine"))
        return object()

    monkeypatch.setattr("tidal_current_grib_generator.cli.download_marine_ie_irish_sea_grib", fake_download)
    output = tmp_path / "environment.grb"

    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-6.0",
            "53.0",
            "-5.5",
            "53.5",
            "--weather-provider",
            "none",
            "--current-source",
            "auto",
            "--hours",
            "24",
            "--output",
            str(output),
            "--metadata-summary",
        ]
    )

    assert rc == 0
    assert "selected current provider: marine_ie_irish_sea" in capsys.readouterr().out


def test_generate_environment_grib_missing_current_inputs(tmp_path: Path, capsys):
    rc = main(
        [
            "generate-environment-grib",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--weather-provider",
            "none",
            "--current-source",
            "existing-file",
            "--hours",
            "3",
            "--output",
            str(tmp_path / "out.grb"),
        ]
    )

    assert rc == 2
    assert "--current-file is required" in capsys.readouterr().err


@pytest.mark.skipif(os.environ.get("CURRENTGRIB_TEST_LIVE_GFS") != "1", reason="live GFS test is opt-in")
def test_live_gfs_tiny_download(tmp_path: Path):
    result = generate_gfs_weather_grib(
        GFSWeatherRequest(
            bbox=BoundingBox(-5.5, 53.0, -5.0, 53.5),
            output=tmp_path / "live-gfs.grb2",
            hours=3,
            step_hours=3,
            cycle="auto",
            overwrite=True,
            timeout_seconds=120,
        )
    )

    assert result.output.exists()
    assert result.inspection["stream_valid"] is True
    assert result.inspection["edition_counts"].get(2, 0) > 0


@pytest.mark.skipif(os.environ.get("CURRENTGRIB_TEST_LIVE_ECMWF") != "1", reason="live ECMWF test is opt-in")
def test_live_ecmwf_tiny_download(tmp_path: Path):
    result = generate_ecmwf_weather_grib(
        ECMWFWeatherRequest(
            bbox=BoundingBox(-5.5, 53.0, -5.0, 53.5),
            output=tmp_path / "live-ecmwf.grb2",
            hours=3,
            step_hours=3,
            cycle="auto",
            overwrite=True,
            timeout_seconds=180,
        )
    )

    assert result.output.exists()
    assert result.inspection["stream_valid"] is True
    assert result.inspection["edition_counts"].get(2, 0) > 0
