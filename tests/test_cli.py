from tidal_current_grib_generator.cli import main


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
