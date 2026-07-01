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
