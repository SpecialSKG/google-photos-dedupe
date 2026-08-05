"""Tests para el metadata_writer (Fase 6): audit + write con ExifTool opcional."""

from pathlib import Path

from PIL import Image

from photos_dedupe.config import Config
from photos_dedupe.metadata_writer import (
    EXIFTOOL_NOT_AVAILABLE,
    NOT_COPIED,
    AUDIT_MISSING,
    AUDIT_OK,
    AUDIT_MISMATCH,
    VERIFY_MISMATCH,
    WRITE_FAILED,
    OK,
    apply_metadata,
    exiftool_binary,
)
from photos_dedupe.planner import PlannedFileOperation


def _image_with_exif(path: Path, dt: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (8, 8), (50, 50, 50))
    exif = img.getexif()
    exif[0x9003] = dt  # DateTimeOriginal
    img.save(path, format="JPEG", exif=exif)
    return path


def _image_no_exif(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (10, 10, 10)).save(path, format="JPEG")
    return path


def _cfg(**overrides) -> Config:
    cfg = Config()
    cfg.metadata_mode = "audit"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_exiftool_binary_none_when_not_installed(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr("photos_dedupe.metadata_writer.os.path.isfile", lambda p: False)
    cfg = _cfg()
    assert exiftool_binary(cfg) is None
    # ruta explícita inválida → None
    cfg.exiftool_path = str(tmp_path / "no-existe.exe")
    assert exiftool_binary(cfg) is None


def test_exiftool_binary_explicit_path(monkeypatch, tmp_path):
    fake = tmp_path / "exiftool.exe"
    fake.write_bytes(b"MZ")
    cfg = _cfg()
    cfg.exiftool_path = str(fake)
    assert exiftool_binary(cfg) == str(fake)


def test_audit_copy_missing(tmp_path):
    f = _image_no_exif(tmp_path / "foto.jpg")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="copied")
    op.planned_destination = f.name
    op.metadata_action = "audit"
    res = apply_metadata(op, _cfg(), tmp_path)
    assert res["status"] == AUDIT_MISSING


def test_audit_copy_ok(tmp_path):
    f = _image_with_exif(tmp_path / "foto.jpg", "2022:10:01 16:05:48")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="copied")
    op.planned_destination = f.name
    op.selected_date = "2022-10-01T16:05:48+00:00"
    op.metadata_action = "audit"
    res = apply_metadata(op, _cfg(), tmp_path)
    assert res["status"] == AUDIT_OK


def test_audit_copy_mismatch(tmp_path):
    f = _image_with_exif(tmp_path / "foto.jpg", "2022:10:01 16:05:48")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="copied")
    op.planned_destination = f.name
    op.selected_date = "2021-05-20T10:00:00+00:00"
    op.metadata_action = "audit"
    res = apply_metadata(op, _cfg(), tmp_path)
    assert res["status"] == AUDIT_MISMATCH


def test_apply_metadata_none_action(tmp_path):
    f = _image_no_exif(tmp_path / "x.jpg")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="copied")
    op.planned_destination = f.name
    op.metadata_action = "none"
    assert apply_metadata(op, _cfg(), tmp_path) == {"status": "none"}


def test_apply_metadata_not_copied_yet(tmp_path):
    f = _image_no_exif(tmp_path / "x.jpg")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="planned")
    op.planned_destination = f.name
    op.metadata_action = "audit"
    res = apply_metadata(op, _cfg(), tmp_path)
    assert res["status"] == NOT_COPIED


def test_write_without_exiftool_returns_not_available(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda name: None)
    # Fuerza a que no exista NINGÚN binario (PATH + rutas estándar ni la explícita)
    monkeypatch.setattr("photos_dedupe.metadata_writer.os.path.isfile", lambda p: False)
    f = _image_no_exif(tmp_path / "foto.jpg")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="copied")
    op.planned_destination = f.name
    op.selected_date = "2022-10-01T16:05:48+00:00"
    op.metadata_action = "write"
    cfg = _cfg(metadata_mode="write", exiftool_path=str(tmp_path / "nope.exe"))
    res = apply_metadata(op, cfg, tmp_path)
    assert res["status"] == EXIFTOOL_NOT_AVAILABLE


def test_write_with_fake_exiftool_fails_cleanly(monkeypatch, tmp_path):
    fake = tmp_path / "exiftool.exe"
    fake.write_bytes(b"not an exe")
    f = _image_no_exif(tmp_path / "foto.jpg")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="copied")
    op.planned_destination = f.name
    op.selected_date = "2022-10-01T16:05:48+00:00"
    op.metadata_action = "write"
    cfg = _cfg(metadata_mode="write", exiftool_path=str(fake), verify_written_metadata=False)
    res = apply_metadata(op, cfg, tmp_path)
    assert res["status"] in (WRITE_FAILED, EXIFTOOL_NOT_AVAILABLE)


# ---------------- e2e con ExifTool REAL instalado (Fase 18) ----------------

import pytest as _pytest

_ET = exiftool_binary(_cfg())
_has_really = _ET is not None


@_pytest.mark.skipif(not _has_really, reason="ExifTool no instalado")
def test_write_real_exiftool_sets_datetimeoriginal(tmp_path):
    f = _image_no_exif(tmp_path / "foto.jpg")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="copied")
    op.planned_destination = f.name
    op.selected_date = "2022-10-01T16:05:48+00:00"
    op.metadata_action = "write"
    cfg = _cfg(metadata_mode="write", verify_written_metadata=True, exiftool_path=_ET)
    res = apply_metadata(op, cfg, tmp_path)
    assert res["status"] == OK, res
    # la copia tiene DateTimeOriginal leíble por nuestro lector (sub-IFD EXIF)
    from photos_dedupe.metadata_writer import _exif_datetime_original
    dt = _exif_datetime_original(tmp_path / "foto.jpg")
    assert dt is not None and dt.strftime("%Y:%m:%d") == "2022:10:01"


@_pytest.mark.skipif(not _has_really, reason="ExifTool no instalado")
def test_write_real_exiftool_verifies_ok(tmp_path):
    """Con verify_written_metadata=True, un write bueno retorna OK (no VERIFY_MISMATCH)."""
    f = _image_no_exif(tmp_path / "foto.jpg")
    op = PlannedFileOperation(file_id=0, source_path=str(f), status="copied")
    op.planned_destination = f.name
    op.selected_date = "2022-10-01T16:05:48+00:00"
    op.metadata_action = "write"
    cfg = _cfg(metadata_mode="write", verify_written_metadata=True, exiftool_path=_ET)
    res = apply_metadata(op, cfg, tmp_path)
    assert res["status"] == OK, res
