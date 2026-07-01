from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging
import os
import sys
import types

import pytest

from tidal_current_grib_generator.api import default_output_filename
from tidal_current_grib_generator.cli import main
from tidal_current_grib_generator.copernicus import CopernicusDownloadRequest, download_copernicus_subset
from tidal_current_grib_generator.dependencies import check_dependencies
from tidal_current_grib_generator.geo import BoundingBox
from tidal_current_grib_generator.providers import ProviderRegistry, select_best_provider_for_bbox
from tidal_current_grib_generator.security import REDACTED, redact_mapping


def test_provider_selection_prefers_nws_for_irish_sea():
    bbox = BoundingBox(-8.5, 50.5, -2.5, 56.5)
    provider = select_best_provider_for_bbox(bbox, registry=ProviderRegistry())
    assert provider is not None
    assert provider.id == "copernicus_nws"


def test_provider_selection_returns_none_when_only_unimplemented_global_matches():
    bbox = BoundingBox(140.0, -30.0, 141.0, -29.0)
    assert select_best_provider_for_bbox(bbox, registry=ProviderRegistry()) is None


def test_redact_mapping_removes_passwords():
    data = redact_mapping({"username": "alice", "password": "secret", "api_token": "token"})
    assert data["username"] == REDACTED
    assert data["password"] == REDACTED
    assert data["api_token"] == REDACTED


def test_default_output_filename():
    now = datetime(2026, 7, 1, 12, 30, tzinfo=timezone.utc)
    assert default_output_filename(now) == "current_grib_20260701_1230.grb"


def test_dependency_check_is_json_serialisable(tmp_path: Path):
    status = check_dependencies(tmp_path).as_dict()
    assert status["python"] is True
    assert status["writable_output_directory"] is True


def test_copernicus_request_safe_summary_redacts_password(tmp_path: Path):
    request = CopernicusDownloadRequest(
        bbox=BoundingBox(-8.5, 50.5, -2.5, 56.5),
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        output_directory=tmp_path,
        output_filename="subset.nc",
        username="user",
        password="secret",
    )
    summary = request.safe_summary()
    assert summary["password"] == REDACTED


def test_copernicus_download_uses_python_api_without_logging_password(monkeypatch, tmp_path: Path):
    calls = {}

    class FakeResponse:
        def model_dump(self, mode="json"):
            return {"ok": True}

    def fake_subset(**kwargs):
        calls["kwargs"] = kwargs
        return FakeResponse()

    fake_module = types.SimpleNamespace(subset=fake_subset)
    monkeypatch.setitem(sys.modules, "copernicusmarine", fake_module)
    monkeypatch.setattr("tidal_current_grib_generator.copernicus.copernicusmarine_available", lambda: True)
    request = CopernicusDownloadRequest(
        bbox=BoundingBox(-8.5, 50.5, -2.5, 56.5),
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        output_directory=tmp_path,
        output_filename="subset.nc",
        username="user",
        password="secret",
        dry_run=True,
    )
    seen = []
    result = download_copernicus_subset(request, progress_callback=lambda step, details: seen.append(details))
    assert result.path == tmp_path / "subset.nc"
    assert calls["kwargs"]["password"] == "secret"
    assert all(details.get("password") != "secret" for details in seen)
    assert "secret" not in str(result.as_dict())


def test_download_copernicus_cli_accepts_hours(monkeypatch, tmp_path: Path, capsys):
    calls = {}

    class FakeResponse:
        def model_dump(self, mode="json"):
            return {"ok": True}

    def fake_subset(**kwargs):
        calls["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setitem(sys.modules, "copernicusmarine", types.SimpleNamespace(subset=fake_subset))
    monkeypatch.setattr("tidal_current_grib_generator.copernicus.copernicusmarine_available", lambda: True)
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "user")
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "download-copernicus",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "5",
            "--output-directory",
            str(tmp_path),
            "--output-filename",
            "subset.nc",
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    assert calls["kwargs"]["end_datetime"].isoformat() == "2026-07-01T05:00:00+00:00"
    assert "secret" not in capsys.readouterr().out


def test_download_copernicus_cli_accepts_end(monkeypatch, tmp_path: Path):
    calls = {}

    class FakeResponse:
        def model_dump(self, mode="json"):
            return {"ok": True}

    def fake_subset(**kwargs):
        calls["kwargs"] = kwargs
        return FakeResponse()

    monkeypatch.setitem(sys.modules, "copernicusmarine", types.SimpleNamespace(subset=fake_subset))
    monkeypatch.setattr("tidal_current_grib_generator.copernicus.copernicusmarine_available", lambda: True)
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "user")
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "download-copernicus",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--start",
            "2026-07-01T00:00:00Z",
            "--end",
            "2026-07-01T03:00:00Z",
            "--output-directory",
            str(tmp_path),
            "--output-filename",
            "subset.nc",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert calls["kwargs"]["end_datetime"].isoformat() == "2026-07-01T03:00:00+00:00"


def test_download_copernicus_cli_rejects_nonpositive_hours(tmp_path: Path, capsys):
    rc = main(
        [
            "download-copernicus",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "0",
            "--output-directory",
            str(tmp_path),
            "--output-filename",
            "subset.nc",
        ]
    )
    assert rc == 2
    assert "--hours must be greater than zero" in capsys.readouterr().err


def test_download_copernicus_cli_requires_end_or_hours(tmp_path: Path):
    with pytest.raises(SystemExit):
        main(
            [
                "download-copernicus",
                "--bbox",
                "-8.5",
                "50.5",
                "-2.5",
                "56.5",
                "--start",
                "2026-07-01T00:00:00Z",
                "--output-directory",
                str(tmp_path),
                "--output-filename",
                "subset.nc",
            ]
        )


def test_download_copernicus_cli_rejects_end_and_hours(tmp_path: Path):
    with pytest.raises(SystemExit):
        main(
            [
                "download-copernicus",
                "--bbox",
                "-8.5",
                "50.5",
                "-2.5",
                "56.5",
                "--start",
                "2026-07-01T00:00:00Z",
                "--end",
                "2026-07-01T03:00:00Z",
                "--hours",
                "3",
                "--output-directory",
                str(tmp_path),
                "--output-filename",
                "subset.nc",
            ]
        )


def test_generate_copernicus_cli_uses_password_env_without_printing(monkeypatch, tmp_path: Path, capsys):
    calls = {}
    download_path = tmp_path / "downloads" / "subset.nc"

    class FakeDownloadResult:
        path = download_path

        def as_dict(self):
            return {"path": str(self.path), "response": {"password": "<redacted>"}}

    class FakeGenerateResult:
        output = tmp_path / "current.grb"
        message_count = 2
        byte_count = 100

        def as_dict(self):
            return {"output": str(self.output), "message_count": self.message_count, "byte_count": self.byte_count}

    def fake_download(request, progress_callback=None):
        calls["download_request"] = request
        return FakeDownloadResult()

    def fake_generate(request, progress_callback=None):
        calls["generate_request"] = request
        return FakeGenerateResult()

    def fake_time_metadata(path):
        first = datetime(2026, 7, 1, tzinfo=timezone.utc)
        return {
            "first_time": first,
            "last_time": first + timedelta(hours=3),
            "time_count": 4,
            "step_hours": 1.0,
            "times": [first + timedelta(hours=i) for i in range(4)],
        }

    monkeypatch.setattr("tidal_current_grib_generator.cli.download_copernicus_subset", fake_download)
    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_current_grib_from_netcdf", fake_generate)
    monkeypatch.setattr("tidal_current_grib_generator.cli.netcdf_time_metadata", fake_time_metadata)
    monkeypatch.setattr("tidal_current_grib_generator.cli.inspect_grib", lambda path: {"message_count": 2})
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "user")
    monkeypatch.setenv("CURRENTGRIB_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "generate-copernicus",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "3",
            "--step-hours",
            "1",
            "--download-directory",
            str(tmp_path / "downloads"),
            "--download-filename",
            "subset.nc",
            "--output",
            str(tmp_path / "current.grb"),
            "--metadata-summary",
        ]
    )
    assert rc == 0
    assert calls["download_request"].password == "secret"
    assert calls["generate_request"].input_netcdf == download_path
    assert calls["generate_request"].hours == 3
    captured = capsys.readouterr()
    assert "secret" not in captured.out
    assert "secret" not in captured.err


def test_generate_copernicus_aligns_to_downloaded_netcdf_times(monkeypatch, tmp_path: Path, capsys):
    calls = {}
    download_path = tmp_path / "downloads" / "subset.nc"

    class FakeDownloadResult:
        path = download_path

        def as_dict(self):
            return {"path": str(self.path)}

    class FakeGenerateResult:
        output = tmp_path / "current.grb"
        message_count = 240
        byte_count = 100

        def as_dict(self):
            return {"output": str(self.output), "message_count": self.message_count, "byte_count": self.byte_count}

    first = datetime(2026, 7, 1, 18, tzinfo=timezone.utc)
    times = [first + timedelta(hours=i) for i in range(120)]

    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.download_copernicus_subset",
        lambda request, progress_callback=None: FakeDownloadResult(),
    )

    def fake_generate(request, progress_callback=None):
        calls["generate_request"] = request
        return FakeGenerateResult()

    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_current_grib_from_netcdf", fake_generate)
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.netcdf_time_metadata",
        lambda path: {
            "first_time": times[0],
            "last_time": times[-1],
            "time_count": len(times),
            "step_hours": 1.0,
            "times": times,
        },
    )
    monkeypatch.setattr("tidal_current_grib_generator.cli.inspect_grib", lambda path: {"message_count": 240})
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "user")
    monkeypatch.setenv("CURRENTGRIB_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "generate-copernicus",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--start",
            "2026-07-01T17:19:57Z",
            "--hours",
            "120",
            "--step-hours",
            "1",
            "--download-directory",
            str(tmp_path / "downloads"),
            "--download-filename",
            "subset.nc",
            "--output",
            str(tmp_path / "current.grb"),
            "--metadata-summary",
        ]
    )
    assert rc == 0
    request = calls["generate_request"]
    assert request.start == datetime(2026, 7, 1, 18, tzinfo=timezone.utc)
    assert request.hours == 119
    assert request.step_hours == 1
    assert request.grid_spacing_deg == 0.03
    output = capsys.readouterr().out
    assert "Requested start time adjusted from 2026-07-01T17:19:57+00:00" in output
    assert "count=120" in output


def test_generate_copernicus_rejects_time_range_outside_download(monkeypatch, tmp_path: Path, capsys):
    first = datetime(2026, 7, 1, 18, tzinfo=timezone.utc)

    class FakeDownloadResult:
        path = tmp_path / "subset.nc"

        def as_dict(self):
            return {"path": str(self.path)}

    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.download_copernicus_subset",
        lambda request, progress_callback=None: FakeDownloadResult(),
    )
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.netcdf_time_metadata",
        lambda path: {
            "first_time": first,
            "last_time": first + timedelta(hours=2),
            "time_count": 3,
            "step_hours": 1.0,
            "times": [first + timedelta(hours=i) for i in range(3)],
        },
    )
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "user")
    monkeypatch.setenv("CURRENTGRIB_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "generate-copernicus",
            "--bbox",
            "-8.5",
            "50.5",
            "-2.5",
            "56.5",
            "--start",
            "2026-07-01T21:00:00Z",
            "--hours",
            "1",
            "--download-directory",
            str(tmp_path),
            "--download-filename",
            "subset.nc",
            "--output",
            str(tmp_path / "current.grb"),
        ]
    )
    assert rc == 2
    assert "requested start is after downloaded NetCDF ends" in capsys.readouterr().err


def test_generate_copernicus_verbose_does_not_emit_third_party_debug(monkeypatch, tmp_path: Path, capsys):
    first = datetime(2026, 7, 1, tzinfo=timezone.utc)

    class FakeDownloadResult:
        path = tmp_path / "subset.nc"

        def as_dict(self):
            return {"path": str(self.path)}

    class FakeGenerateResult:
        output = tmp_path / "current.grb"
        message_count = 8
        byte_count = 100

        def as_dict(self):
            return {"output": str(self.output), "message_count": self.message_count, "byte_count": self.byte_count}

    def fake_download(request, progress_callback=None):
        logging.getLogger("urllib3.connectionpool").debug("debug url x-cop-user=alice@example.com")
        if progress_callback:
            progress_callback("download complete", {"path": str(tmp_path / "subset.nc")})
        return FakeDownloadResult()

    def fake_generate(request, progress_callback=None):
        if progress_callback:
            progress_callback("generating timestep", {"index": 4, "time": "2026-07-01T03:00:00+00:00"})
        return FakeGenerateResult()

    monkeypatch.setattr("tidal_current_grib_generator.cli.download_copernicus_subset", fake_download)
    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_current_grib_from_netcdf", fake_generate)
    monkeypatch.setattr("tidal_current_grib_generator.cli.inspect_grib", lambda path: {"message_count": 8})
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.netcdf_time_metadata",
        lambda path: {
            "first_time": first,
            "last_time": first + timedelta(hours=3),
            "time_count": 4,
            "step_hours": 1.0,
            "times": [first + timedelta(hours=i) for i in range(4)],
        },
    )
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "alice@example.com")
    monkeypatch.setenv("CURRENTGRIB_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "generate-copernicus",
            "--bbox",
            "-5.5",
            "53.0",
            "-5.0",
            "53.5",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "3",
            "--download-directory",
            str(tmp_path),
            "--output",
            str(tmp_path / "current.grb"),
            "--metadata-summary",
            "--verbose",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "wrote 8 messages through valid time" in combined
    assert "urllib3" not in combined
    assert "alice@example.com" not in combined
    assert "secret" not in combined
    assert "x-cop-user=alice" not in combined


def test_generate_copernicus_debug_enables_diagnostic_logs(monkeypatch, tmp_path: Path, capsys):
    first = datetime(2026, 7, 1, tzinfo=timezone.utc)

    class FakeDownloadResult:
        path = tmp_path / "subset.nc"

        def as_dict(self):
            return {"path": str(self.path)}

    class FakeGenerateResult:
        output = tmp_path / "current.grb"
        message_count = 8
        byte_count = 100

        def as_dict(self):
            return {"output": str(self.output), "message_count": self.message_count, "byte_count": self.byte_count}

    def fake_download(request, progress_callback=None):
        logging.getLogger("urllib3.connectionpool").debug("debug url https://example.invalid/?x-cop-user=alice@example.com")
        return FakeDownloadResult()

    monkeypatch.setattr("tidal_current_grib_generator.cli.download_copernicus_subset", fake_download)
    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_current_grib_from_netcdf", lambda request, progress_callback=None: FakeGenerateResult())
    monkeypatch.setattr("tidal_current_grib_generator.cli.inspect_grib", lambda path: {"message_count": 8})
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.netcdf_time_metadata",
        lambda path: {
            "first_time": first,
            "last_time": first + timedelta(hours=3),
            "time_count": 4,
            "step_hours": 1.0,
            "times": [first + timedelta(hours=i) for i in range(4)],
        },
    )
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "alice@example.com")
    monkeypatch.setenv("CURRENTGRIB_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "generate-copernicus",
            "--bbox",
            "-5.5",
            "53.0",
            "-5.0",
            "53.5",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "3",
            "--download-directory",
            str(tmp_path),
            "--output",
            str(tmp_path / "current.grb"),
            "--debug",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "debug url" in combined
    assert "x-cop-user=<redacted>" in combined
    assert "alice@example.com" not in combined
    assert "secret" not in combined


@pytest.mark.skipif(
    not (
        os.environ.get("CURRENTGRIB_TEST_COPERNICUS_USERNAME")
        and os.environ.get("CURRENTGRIB_TEST_COPERNICUS_PASSWORD")
    ),
    reason="live Copernicus credentials not configured",
)
def test_live_copernicus_small_subset_smoke(tmp_path: Path):
    request = CopernicusDownloadRequest(
        bbox=BoundingBox(-5.1, 53.2, -5.0, 53.3),
        start=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
        output_directory=tmp_path,
        output_filename="live_subset.nc",
        username=os.environ["CURRENTGRIB_TEST_COPERNICUS_USERNAME"],
        password=os.environ["CURRENTGRIB_TEST_COPERNICUS_PASSWORD"],
    )
    result = download_copernicus_subset(request)
    assert result.path.exists()
