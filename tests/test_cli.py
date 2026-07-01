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
