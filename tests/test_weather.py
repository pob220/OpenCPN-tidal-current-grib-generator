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
    GFSWeatherRequest,
    build_gfs_filter_url,
    forecast_hour_sequence,
    generate_ecmwf_weather_grib,
    generate_gfs_weather_grib,
    gfs_cycle_candidates,
    list_weather_providers,
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
    assert {"gfs", "ecmwf_ifs_open", "dwd_icon_eu"} <= set(by_id)
    assert by_id["gfs"].source == "NOAA NOMADS"
    assert by_id["gfs"].format == "GRIB2"
    assert by_id["gfs"].account == "free/no account"
    assert by_id["ecmwf_ifs_open"].source == "ECMWF Open Data"
    assert by_id["ecmwf_ifs_open"].implemented is True
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
    assert "ecmwf_ifs_open: ECMWF IFS Open Data forecast" in out
    assert "source: NOAA NOMADS" in out


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
