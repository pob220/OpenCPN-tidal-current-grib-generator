from datetime import datetime, timedelta, timezone
from pathlib import Path
import ftplib
import logging
import os
import sys
import types

import numpy as np
import pytest

from tidal_current_grib_generator.api import default_output_filename
from tidal_current_grib_generator.cli import main
from tidal_current_grib_generator.copernicus import CopernicusDownloadRequest, download_copernicus_subset
from tidal_current_grib_generator.dependencies import check_dependencies
from tidal_current_grib_generator.geo import BoundingBox
from tidal_current_grib_generator.marine_ie import (
    _download_ftp,
    download_marine_ie_irish_sea_grib,
    validate_direct_current_grib,
)
from tidal_current_grib_generator.providers import ProviderRegistry, select_best_provider_for_bbox
from tidal_current_grib_generator.security import REDACTED, redact_mapping
from tidal_current_grib_generator.sources.netcdf import _regular_spacing


def test_provider_selection_prefers_nws_for_irish_sea():
    bbox = BoundingBox(-8.5, 50.5, -2.5, 56.5)
    provider = select_best_provider_for_bbox(bbox, registry=ProviderRegistry())
    assert provider is not None
    assert provider.id == "copernicus_nws"


def test_provider_registry_includes_marine_ie_provider():
    provider = ProviderRegistry().get("marine_ie_irish_sea")
    assert provider.implemented is True
    assert provider.provider_type == "direct_current_grib"
    assert provider.nominal_duration_hours == 72
    assert provider.default_step_hours == 1


def test_provider_selection_prefers_marine_ie_inside_coverage_for_72h():
    bbox = BoundingBox(-6.0, 52.0, -5.0, 54.0)
    provider = select_best_provider_for_bbox(bbox, duration_hours=72, registry=ProviderRegistry())
    assert provider is not None
    assert provider.id == "marine_ie_irish_sea"


def test_provider_selection_skips_marine_ie_for_longer_duration():
    bbox = BoundingBox(-6.0, 52.0, -5.0, 54.0)
    provider = select_best_provider_for_bbox(bbox, duration_hours=73, registry=ProviderRegistry())
    assert provider is not None
    assert provider.id == "copernicus_nws"


def test_provider_selection_skips_marine_ie_outside_coverage():
    bbox = BoundingBox(-8.5, 50.5, -2.5, 56.5)
    provider = select_best_provider_for_bbox(bbox, duration_hours=72, registry=ProviderRegistry())
    assert provider is not None
    assert provider.id == "copernicus_nws"


def test_provider_registry_includes_global_provider():
    provider = ProviderRegistry().get("copernicus_global")
    assert provider.implemented is True
    assert provider.dataset_id == "cmems_mod_glo_phy_anfc_0.083deg_PT1H-m"
    assert provider.variables == ("uo", "vo")
    assert provider.source_grid_regularity_tolerance == pytest.approx(5e-5)
    assert provider.maximum_depth == pytest.approx(0.5)


def test_provider_registry_keeps_nws_source_grid_tolerance():
    provider = ProviderRegistry().get("copernicus_nws")
    assert provider.source_grid_regularity_tolerance == pytest.approx(1e-5)


def test_global_source_grid_tolerance_accepts_copernicus_float_noise():
    coordinates = np.array(
        [-40.5, -40.4166717529297, -40.3333435058594, -40.25, -40.1666564941406, -40.0833282470703],
        dtype=np.float32,
    )
    spacing = _regular_spacing(
        coordinates,
        "longitude",
        ProviderRegistry().get("copernicus_global").source_grid_regularity_tolerance,
    )
    assert spacing == pytest.approx(0.0833282470703)


def test_provider_selection_uses_global_outside_nws():
    bbox = BoundingBox(140.0, -30.0, 141.0, -29.0)
    provider = select_best_provider_for_bbox(bbox, registry=ProviderRegistry())
    assert provider is not None
    assert provider.id == "copernicus_global"


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


def test_generate_provider_downloads_marine_ie_with_mock(monkeypatch, tmp_path: Path, capsys):
    calls = {}

    class FakeResult:
        output = tmp_path / "marine.grb"
        inspection = {
            "message_count": 2,
            "byte_count": 24,
            "current_component_counts": {"u_49": 1, "v_50": 1},
        }

        def as_dict(self):
            return {"output": str(self.output), "inspection": self.inspection}

    def fake_download(output, overwrite=False, progress_callback=None):
        calls["output"] = output
        calls["overwrite"] = overwrite
        if progress_callback:
            progress_callback("download complete", {"output": str(output), "message_count": 2, "byte_count": 24})
        return FakeResult()

    monkeypatch.setattr("tidal_current_grib_generator.cli.download_marine_ie_irish_sea_grib", fake_download)
    rc = main(
        [
            "generate-provider",
            "--provider",
            "marine_ie_irish_sea",
            "--output",
            str(tmp_path / "marine.grb"),
            "--overwrite",
            "--metadata-summary",
            "--verbose",
        ]
    )
    assert rc == 0
    assert calls["output"] == tmp_path / "marine.grb"
    assert calls["overwrite"] is True
    output = capsys.readouterr().out
    assert "Marine Institute Ireland Irish Sea currents" in output
    assert "validated GRIB stream: 2 messages" in output


def test_generate_provider_auto_selects_marine_ie(monkeypatch, tmp_path: Path, capsys):
    class FakeResult:
        output = tmp_path / "marine.grb"
        inspection = {"message_count": 2, "byte_count": 24}

        def as_dict(self):
            return {"output": str(self.output), "inspection": self.inspection}

    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.download_marine_ie_irish_sea_grib",
        lambda output, overwrite=False, progress_callback=None: FakeResult(),
    )
    rc = main(
        [
            "generate-provider",
            "--provider",
            "auto",
            "--bbox",
            "-6.0",
            "52.0",
            "-5.0",
            "54.0",
            "--hours",
            "72",
            "--output",
            str(tmp_path / "marine.grb"),
            "--metadata-summary",
        ]
    )
    assert rc == 0
    assert "selected provider: marine_ie_irish_sea" in capsys.readouterr().out


def test_direct_grib_validation_rejects_bad_stream(tmp_path: Path):
    path = tmp_path / "bad.grb"
    path.write_bytes(b"not grib")
    with pytest.raises(Exception):
        validate_direct_current_grib(path)


def test_direct_grib_validation_accepts_mock_current_stream(monkeypatch, tmp_path: Path):
    path = tmp_path / "mock.grb"
    path.write_bytes(b"GRIB" + (12).to_bytes(3, "big") + bytes([1]) + b"7777")
    monkeypatch.setattr(
        "tidal_current_grib_generator.marine_ie.inspect_grib",
        lambda p: {
            "message_count": 1,
            "byte_count": 12,
            "edition_counts": {1: 1},
            "stream_valid": True,
            "eccodes_available": True,
            "current_component_counts": {"u_49": 1, "v_50": 1},
        },
    )
    inspection = validate_direct_current_grib(path)
    assert inspection["current_component_counts"] == {"u_49": 1, "v_50": 1}


def test_marine_ie_provider_normalizes_raw_download(monkeypatch, tmp_path: Path):
    message = b"GRIB" + (12).to_bytes(3, "big") + b"\x01" + b"7777"

    def fake_download(file_obj, timeout_seconds):
        file_obj.write(b"header" + message + b"\r\r\n" + message + b"tail")

    monkeypatch.setattr("tidal_current_grib_generator.marine_ie._download_ftp", fake_download)
    monkeypatch.setattr(
        "tidal_current_grib_generator.marine_ie.inspect_grib",
        lambda p: {
            "message_count": 2,
            "byte_count": 24,
            "edition_counts": {1: 2},
            "stream_valid": True,
            "eccodes_available": True,
            "current_component_counts": {"u_49": 1, "v_50": 1},
        },
    )
    output = tmp_path / "marine.grb"
    result = download_marine_ie_irish_sea_grib(output, overwrite=True)
    assert output.read_bytes() == message + message
    assert result.inspection["raw_byte_count"] == len(b"header" + message + b"\r\r\n" + message + b"tail")
    assert result.inspection["clean_byte_count"] == 24
    assert result.inspection["skipped_byte_count"] == len(b"header\r\r\ntail")
    assert result.inspection["extracted_message_count"] == 2


def test_marine_ie_provider_removes_temp_output_on_validation_failure(monkeypatch, tmp_path: Path):
    def fake_download(file_obj, timeout_seconds):
        file_obj.write(b"not grib")

    monkeypatch.setattr("tidal_current_grib_generator.marine_ie._download_ftp", fake_download)
    output = tmp_path / "marine.grb"
    with pytest.raises(Exception):
        download_marine_ie_irish_sea_grib(output, overwrite=True)
    assert not output.exists()


class FakeMarineFTP:
    instances = []

    def __init__(self, *args, **kwargs):
        self.cwd_calls = []
        self.retr_calls = []
        self.payload = b"payload"
        self.base_pwd = "/D:/SFTPDATA/OSS"
        self.fail_cwd = set()
        self.fail_retr = set()
        self.login_failed = False
        FakeMarineFTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def connect(self, host, port, timeout=None):
        self.host = host
        self.port = port

    def login(self, user, password):
        if self.login_failed:
            raise ftplib.error_perm("530 login incorrect")

    def set_pasv(self, enabled):
        self.passive = enabled

    def pwd(self):
        return self.base_pwd

    def cwd(self, path):
        self.cwd_calls.append(path)
        if path in self.fail_cwd:
            raise ftplib.error_perm(f"550 {path}")

    def retrbinary(self, command, callback):
        self.retr_calls.append(command)
        if command in self.fail_retr:
            raise ftplib.error_perm(f"550 {command}")
        callback(self.payload)


def test_marine_ie_ftp_relative_cwd_success(monkeypatch):
    FakeMarineFTP.instances = []
    monkeypatch.setattr("tidal_current_grib_generator.marine_ie.ftplib.FTP", FakeMarineFTP)
    sink = bytearray()
    _download_ftp(types.SimpleNamespace(write=sink.extend), 60)
    ftp = FakeMarineFTP.instances[0]
    assert ftp.passive is True
    assert ftp.cwd_calls == ["OSS/modelling/GRIB_Files"]
    assert ftp.retr_calls == ["RETR irish_sea_ms.grb"]
    assert sink == b"payload"


def test_marine_ie_ftp_absolute_cwd_not_used_when_relative_succeeds(monkeypatch):
    FakeMarineFTP.instances = []
    monkeypatch.setattr("tidal_current_grib_generator.marine_ie.ftplib.FTP", FakeMarineFTP)
    _download_ftp(types.SimpleNamespace(write=lambda data: None), 60)
    ftp = FakeMarineFTP.instances[0]
    assert "/OSS/modelling/GRIB_Files" not in ftp.cwd_calls
    assert "/" not in ftp.cwd_calls


def test_marine_ie_ftp_fallback_resets_to_base_not_slash(monkeypatch):
    FakeMarineFTP.instances = []

    class FallbackFTP(FakeMarineFTP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fail_cwd = {"OSS/modelling/GRIB_Files"}

    monkeypatch.setattr("tidal_current_grib_generator.marine_ie.ftplib.FTP", FallbackFTP)
    sink = bytearray()
    _download_ftp(types.SimpleNamespace(write=sink.extend), 60)
    ftp = FakeMarineFTP.instances[0]
    assert ftp.cwd_calls == ["OSS/modelling/GRIB_Files", "/D:/SFTPDATA/OSS"]
    assert "/" not in ftp.cwd_calls
    assert ftp.retr_calls == ["RETR OSS/modelling/GRIB_Files/irish_sea_ms.grb"]
    assert sink == b"payload"


def test_marine_ie_ftp_retrieval_failure_reports_strategies(monkeypatch):
    FakeMarineFTP.instances = []

    class FailingFTP(FakeMarineFTP):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.fail_retr = {
                "RETR irish_sea_ms.grb",
                "RETR OSS/modelling/GRIB_Files/irish_sea_ms.grb",
            }

        def cwd(self, path):
            super().cwd(path)
            if path in {"OSS", "modelling", "GRIB_Files"}:
                return

    monkeypatch.setattr("tidal_current_grib_generator.marine_ie.ftplib.FTP", FailingFTP)
    with pytest.raises(Exception, match="relative cwd OSS/modelling/GRIB_Files"):
        _download_ftp(types.SimpleNamespace(write=lambda data: None), 60)
    ftp = FakeMarineFTP.instances[0]
    assert "/" not in ftp.cwd_calls


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


def test_download_copernicus_auto_uses_global_outside_nws(monkeypatch, tmp_path: Path):
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
            "140.0",
            "-30.0",
            "141.0",
            "-29.0",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "6",
            "--output-directory",
            str(tmp_path),
            "--output-filename",
            "global_subset.nc",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert calls["kwargs"]["dataset_id"] == "cmems_mod_glo_phy_anfc_0.083deg_PT1H-m"
    assert calls["kwargs"]["variables"] == ["uo", "vo"]
    assert calls["kwargs"]["minimum_depth"] == 0.0
    assert calls["kwargs"]["maximum_depth"] == 0.5


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


def test_generate_copernicus_explicit_global_uses_global_dataset(monkeypatch, tmp_path: Path):
    calls = {}
    first = datetime(2026, 7, 1, tzinfo=timezone.utc)
    download_path = tmp_path / "downloads" / "global_subset.nc"

    class FakeDownloadResult:
        path = download_path

        def as_dict(self):
            return {"path": str(self.path), "dataset_id": calls["download_request"].dataset_id}

    class FakeGenerateResult:
        output = tmp_path / "global.grb"
        message_count = 14
        byte_count = 100

        def as_dict(self):
            return {"output": str(self.output), "message_count": self.message_count, "byte_count": self.byte_count}

    def fake_download(request, progress_callback=None):
        calls["download_request"] = request
        return FakeDownloadResult()

    def fake_generate(request, progress_callback=None):
        calls["generate_request"] = request
        return FakeGenerateResult()

    monkeypatch.setattr("tidal_current_grib_generator.cli.download_copernicus_subset", fake_download)
    monkeypatch.setattr("tidal_current_grib_generator.cli.generate_current_grib_from_netcdf", fake_generate)
    monkeypatch.setattr("tidal_current_grib_generator.cli.inspect_grib", lambda path: {"message_count": 14})
    monkeypatch.setattr(
        "tidal_current_grib_generator.cli.netcdf_time_metadata",
        lambda path: {
            "first_time": first,
            "last_time": first + timedelta(hours=6),
            "time_count": 7,
            "step_hours": 1.0,
            "times": [first + timedelta(hours=i) for i in range(7)],
        },
    )
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "user")
    monkeypatch.setenv("CURRENTGRIB_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "generate-copernicus",
            "--provider",
            "copernicus_global",
            "--bbox",
            "140.0",
            "-30.0",
            "141.0",
            "-29.0",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "6",
            "--download-directory",
            str(tmp_path / "downloads"),
            "--download-filename",
            "global_subset.nc",
            "--output",
            str(tmp_path / "global.grb"),
            "--metadata-summary",
        ]
    )
    assert rc == 0
    download_request = calls["download_request"]
    assert download_request.dataset_id == "cmems_mod_glo_phy_anfc_0.083deg_PT1H-m"
    assert download_request.variables == ("uo", "vo")
    assert download_request.minimum_depth == 0.0
    assert download_request.maximum_depth == 0.5
    assert calls["generate_request"].step_hours == 1
    assert calls["generate_request"].hours == 6
    assert calls["generate_request"].source_grid_regularity_tolerance == pytest.approx(5e-5)


def test_generate_copernicus_allows_explicit_source_grid_tolerance(monkeypatch, tmp_path: Path):
    calls = {}
    first = datetime(2026, 7, 1, tzinfo=timezone.utc)

    class FakeDownloadResult:
        path = tmp_path / "downloads" / "subset.nc"

        def as_dict(self):
            return {"path": str(self.path)}

    class FakeGenerateResult:
        output = tmp_path / "current.grb"
        message_count = 8
        byte_count = 100

        def as_dict(self):
            return {"output": str(self.output), "message_count": self.message_count, "byte_count": self.byte_count}

    monkeypatch.setattr("tidal_current_grib_generator.cli.download_copernicus_subset", lambda request, progress_callback=None: FakeDownloadResult())

    def fake_generate(request, progress_callback=None):
        calls["generate_request"] = request
        return FakeGenerateResult()

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
    monkeypatch.setenv("CURRENTGRIB_TEST_COPERNICUS_USERNAME", "user")
    monkeypatch.setenv("CURRENTGRIB_COPERNICUS_PASSWORD", "secret")
    rc = main(
        [
            "generate-copernicus",
            "--provider",
            "copernicus_global",
            "--bbox",
            "-40.5",
            "30.0",
            "-40.0",
            "30.5",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "3",
            "--source-grid-regularity-tolerance",
            "0.0002",
            "--download-directory",
            str(tmp_path / "downloads"),
            "--output",
            str(tmp_path / "current.grb"),
        ]
    )
    assert rc == 0
    assert calls["generate_request"].source_grid_regularity_tolerance == pytest.approx(0.0002)


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
