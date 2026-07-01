from datetime import datetime, timezone
from pathlib import Path
import os
import sys
import types

import pytest

from tidal_current_grib_generator.api import default_output_filename
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
    assert data["username"] == "alice"
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
