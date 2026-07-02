from pathlib import Path

from tidal_current_grib_generator.cli import main
from tidal_current_grib_generator.grib.writer import GribWriteSummary


def test_cli_dry_run(capsys):
    rc = main(
        [
            "generate",
            "--bbox",
            "-7.0",
            "51.5",
            "-6.5",
            "52.0",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "2",
            "--step-hours",
            "1",
            "--grid-spacing-deg",
            "0.25",
            "--source",
            "synthetic",
            "--output",
            "dry-run.grb",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert "messages: 6" in capsys.readouterr().out


def test_cli_sample_point_synthetic(capsys):
    rc = main(
        [
            "sample-point",
            "--source",
            "synthetic",
            "--lat",
            "53.3",
            "--lon",
            "-5.0",
            "--time",
            "2026-07-01T12:00:00Z",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "u_mps:" in out
    assert "direction_degrees_true_toward:" in out


def test_cli_inspect_source_synthetic(capsys):
    rc = main(["inspect-source", "--source", "synthetic"])
    assert rc == 0
    assert "name: synthetic" in capsys.readouterr().out


def test_cli_tpxo_without_model_dir_errors(capsys):
    rc = main(
        [
            "generate",
            "--bbox",
            "-7.0",
            "51.5",
            "-6.5",
            "52.0",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "2",
            "--step-hours",
            "1",
            "--grid-spacing-deg",
            "0.25",
            "--source",
            "tpxo",
            "--output",
            "dry-run.grb",
            "--dry-run",
        ]
    )
    assert rc == 2
    assert "--model-dir is required" in capsys.readouterr().err


def test_cli_tpxo_dry_run_labels_as_astronomical_tide_model(tmp_path: Path, capsys):
    rc = main(
        [
            "generate",
            "--bbox",
            "-6.0",
            "53.0",
            "-5.5",
            "53.5",
            "--start",
            "2026-07-04T00:00:00Z",
            "--hours",
            "6",
            "--step-hours",
            "1",
            "--grid-spacing-deg",
            "0.05",
            "--source",
            "tpxo",
            "--model-dir",
            str(tmp_path),
            "--model-name",
            "TPXO10-atlas-v2-nc",
            "--output",
            "dry-run.grb",
            "--dry-run",
        ]
    )
    assert rc == 0
    assert "Source: TPXO10 astronomical tide model" in capsys.readouterr().out


def test_cli_tpxo_workers_rejects_non_tpxo_source(capsys):
    rc = main(
        [
            "generate",
            "--bbox",
            "-7.0",
            "51.5",
            "-6.5",
            "52.0",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "2",
            "--step-hours",
            "1",
            "--grid-spacing-deg",
            "0.25",
            "--source",
            "synthetic",
            "--tpxo-workers",
            "2",
            "--output",
            "dry-run.grb",
            "--dry-run",
        ]
    )
    assert rc == 2
    assert "--tpxo-workers is only supported with --source tpxo" in capsys.readouterr().err


def test_cli_tpxo_workers_rejects_invalid_count(tmp_path: Path, capsys):
    rc = main(
        [
            "generate",
            "--bbox",
            "-6.0",
            "53.0",
            "-5.5",
            "53.5",
            "--start",
            "2026-07-04T00:00:00Z",
            "--hours",
            "6",
            "--step-hours",
            "1",
            "--grid-spacing-deg",
            "0.05",
            "--source",
            "tpxo",
            "--model-dir",
            str(tmp_path),
            "--tpxo-workers",
            "0",
            "--output",
            "dry-run.grb",
            "--dry-run",
        ]
    )
    assert rc == 2
    assert "--tpxo-workers must be 1 or greater" in capsys.readouterr().err


def test_cli_tpxo_workers_rejects_parallel_count(tmp_path: Path, capsys):
    rc = main(
        [
            "generate",
            "--bbox",
            "-6.0",
            "53.0",
            "-5.5",
            "53.5",
            "--start",
            "2026-07-04T00:00:00Z",
            "--hours",
            "6",
            "--step-hours",
            "1",
            "--grid-spacing-deg",
            "0.05",
            "--source",
            "tpxo",
            "--model-dir",
            str(tmp_path),
            "--tpxo-workers",
            "2",
            "--output",
            "dry-run.grb",
            "--dry-run",
        ]
    )
    assert rc == 2
    assert "parallel TPXO workers are disabled" in capsys.readouterr().err


def test_cli_prepare_tpxo_cache_parses_arguments(monkeypatch, tmp_path: Path, capsys):
    calls = {}

    class FakePrepared:
        path = tmp_path / "cache.tpxocache"
        preparation_seconds = 1.25
        point_count = 4

        class grid:
            nx = 2
            ny = 2

        class metadata:
            constituents = ["m2"]

        def summary(self):
            return {"cache_file": str(self.path)}

    def fake_prepare(**kwargs):
        calls.update(kwargs)
        return FakePrepared()

    monkeypatch.setattr("tidal_current_grib_generator.cli.prepare_tpxo_cache", fake_prepare)
    rc = main(
        [
            "prepare-tpxo-cache",
            "--bbox",
            "-1",
            "50",
            "-0.9",
            "50.1",
            "--grid-spacing-deg",
            "0.1",
            "--model-dir",
            str(tmp_path),
            "--model-name",
            "TPXO10-atlas-v2-nc",
            "--output",
            str(tmp_path / "cache.tpxocache"),
        ]
    )

    assert rc == 0
    assert calls["model_directory"] == tmp_path
    assert calls["output"] == tmp_path / "cache.tpxocache"
    assert "wrote TPXO cache" in capsys.readouterr().out


def test_cli_tpxo_cache_requires_input_cache(capsys):
    rc = main(
        [
            "generate",
            "--source",
            "tpxo-cache",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "1",
            "--step-hours",
            "1",
            "--output",
            "out.grb",
            "--dry-run",
        ]
    )
    assert rc == 2
    assert "--input-cache is required" in capsys.readouterr().err


def test_generate_discards_incomplete_temp_output(monkeypatch, tmp_path: Path, capsys):
    class FakeWriter:
        def write(self, grids, output, progress_callback=None):
            next(iter(grids))
            message = b"GRIB" + (12).to_bytes(3, "big") + b"\x01" + b"7777"
            output.write_bytes(message * 2)
            return GribWriteSummary(message_count=2, output=output)

    monkeypatch.setattr("tidal_current_grib_generator.cli.EccodesGrib1CurrentWriter", FakeWriter)
    output = tmp_path / "incomplete.grb"
    rc = main(
        [
            "generate",
            "--bbox",
            "-7.0",
            "51.5",
            "-6.5",
            "52.0",
            "--start",
            "2026-07-01T00:00:00Z",
            "--hours",
            "2",
            "--step-hours",
            "1",
            "--grid-spacing-deg",
            "0.25",
            "--source",
            "synthetic",
            "--output",
            str(output),
        ]
    )
    assert rc == 2
    assert not output.exists()
    assert "expected 6" in capsys.readouterr().err
