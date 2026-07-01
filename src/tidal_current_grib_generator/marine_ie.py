"""Marine Institute Ireland direct-current GRIB provider."""

from __future__ import annotations

import ftplib
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tidal_current_grib_generator.errors import ValidationError
from tidal_current_grib_generator.grib.validation import inspect_grib, normalize_grib_stream, scan_grib_messages

ProgressCallback = Callable[[str, dict[str, Any]], None]

MARINE_IE_HOST = "ftp.marine.ie"
MARINE_IE_USERNAME = "ftpossapp2"
MARINE_IE_PASSWORD = "FtpOssapp2"
MARINE_IE_PATH = "/OSS/modelling/GRIB_Files/irish_sea_ms.grb"
MARINE_IE_RELATIVE_DIRECTORY = "OSS/modelling/GRIB_Files"
MARINE_IE_RELATIVE_PATH = "OSS/modelling/GRIB_Files/irish_sea_ms.grb"
MARINE_IE_FILENAME = "irish_sea_ms.grb"


@dataclass(frozen=True)
class DirectGribDownloadResult:
    output: Path
    inspection: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"output": str(self.output), "inspection": self.inspection}


def download_marine_ie_irish_sea_grib(
    output: Path,
    overwrite: bool = False,
    timeout_seconds: int = 60,
    progress_callback: ProgressCallback | None = None,
) -> DirectGribDownloadResult:
    output = output.expanduser()
    if output.exists() and not overwrite:
        raise ValidationError(f"output file exists; use --overwrite to replace it: {output}")
    if output.exists() and output.is_dir():
        raise ValidationError(f"output path is a directory: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    if progress_callback:
        progress_callback("downloading Marine Institute GRIB", {"host": MARINE_IE_HOST, "path": MARINE_IE_PATH})

    raw_path: Path | None = None
    clean_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix=output.name + ".raw.", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            raw_path = Path(tmp.name)
            _download_ftp(tmp, timeout_seconds)
        with tempfile.NamedTemporaryFile(prefix=output.name + ".clean.", suffix=".tmp", dir=output.parent, delete=False) as tmp:
            clean_path = Path(tmp.name)
        normalize = normalize_grib_stream(raw_path, clean_path)
        inspection = validate_direct_current_grib(clean_path)
        inspection["raw_byte_count"] = normalize.raw_byte_count
        inspection["clean_byte_count"] = normalize.clean_byte_count
        inspection["skipped_byte_count"] = normalize.skipped_byte_count
        inspection["extracted_message_count"] = normalize.message_count
        shutil.move(str(clean_path), str(output))
        clean_path = None
        if progress_callback:
            progress_callback(
                "download complete",
                {
                    "output": str(output),
                    "message_count": inspection.get("message_count"),
                    "byte_count": inspection.get("byte_count"),
                    "raw_byte_count": inspection.get("raw_byte_count"),
                    "clean_byte_count": inspection.get("clean_byte_count"),
                    "skipped_byte_count": inspection.get("skipped_byte_count"),
                },
            )
        return DirectGribDownloadResult(output=output, inspection=inspection)
    except OSError as exc:
        raise ValidationError(f"Marine Institute FTP download failed: {exc}") from exc
    finally:
        for path in (raw_path, clean_path):
            if path is not None and path.exists():
                path.unlink()


def validate_direct_current_grib(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ValidationError(f"GRIB file does not exist: {path}")
    if path.stat().st_size <= 0:
        raise ValidationError(f"GRIB file is empty: {path}")
    scan = scan_grib_messages(path)
    if scan.message_count <= 0:
        raise ValidationError(f"GRIB file contains no messages: {path}")
    inspection = inspect_grib(path)
    if inspection.get("eccodes_available"):
        counts = inspection.get("current_component_counts", {})
        if int(counts.get("u_49", 0)) <= 0 or int(counts.get("v_50", 0)) <= 0:
            raise ValidationError(
                "GRIB stream is valid but current component parameters 49 and 50 were not both detected"
            )
    else:
        inspection["current_component_validation"] = "skipped: ecCodes not available"
    return inspection


def _download_ftp(file_obj: Any, timeout_seconds: int) -> None:
    with ftplib.FTP(timeout=timeout_seconds) as ftp:
        try:
            ftp.connect(MARINE_IE_HOST, 21, timeout=timeout_seconds)
        except OSError as exc:
            raise ValidationError(f"Marine Institute FTP connection failed: {exc}") from exc
        try:
            ftp.login(MARINE_IE_USERNAME, MARINE_IE_PASSWORD)
        except ftplib.error_perm as exc:
            raise ValidationError("Marine Institute FTP login failed") from exc
        ftp.set_pasv(True)
        base_pwd = ftp.pwd()
        errors: list[str] = []

        if _try_retrieve_from_relative_directory(ftp, file_obj, errors):
            return
        if _try_retrieve_relative_path_from_base(ftp, file_obj, base_pwd, errors):
            return
        if _try_retrieve_stepwise_from_base(ftp, file_obj, base_pwd, errors):
            return

        raise ValidationError("Marine Institute FTP retrieval failed; tried " + "; ".join(errors))


def _try_retrieve_from_relative_directory(ftp: ftplib.FTP, file_obj: Any, errors: list[str]) -> bool:
    strategy = "relative cwd OSS/modelling/GRIB_Files"
    try:
        ftp.cwd(MARINE_IE_RELATIVE_DIRECTORY)
        ftp.retrbinary(f"RETR {MARINE_IE_FILENAME}", file_obj.write)
        return True
    except ftplib.all_errors as exc:
        errors.append(f"{strategy}: {exc}")
        return False


def _try_retrieve_relative_path_from_base(
    ftp: ftplib.FTP,
    file_obj: Any,
    base_pwd: str,
    errors: list[str],
) -> bool:
    strategy = "relative RETR path from login directory"
    try:
        ftp.cwd(base_pwd)
        ftp.retrbinary(f"RETR {MARINE_IE_RELATIVE_PATH}", file_obj.write)
        return True
    except ftplib.all_errors as exc:
        errors.append(f"{strategy}: {exc}")
        return False


def _try_retrieve_stepwise_from_base(
    ftp: ftplib.FTP,
    file_obj: Any,
    base_pwd: str,
    errors: list[str],
) -> bool:
    strategy = "stepwise relative cwd from login directory"
    try:
        ftp.cwd(base_pwd)
        for part in ("OSS", "modelling", "GRIB_Files"):
            ftp.cwd(part)
        ftp.retrbinary(f"RETR {MARINE_IE_FILENAME}", file_obj.write)
        return True
    except ftplib.all_errors as exc:
        errors.append(f"{strategy}: {exc}")
        return False
