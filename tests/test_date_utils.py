"""Tests para extracción de fecha y asignación de cuenta."""

import json
import os
from pathlib import Path

from PIL import Image, ExifTags

from photos_dedupe.date_utils import (
    find_takeout_sidecar_json,
    get_capture_datetime,
    get_capture_year,
    get_capture_year_for_group,
    get_date_source_used,
    infer_account,
)


def _write_sidecar(media: Path, ts: int) -> Path:
    sidecar = media.with_name(media.name + ".json")
    sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": str(ts)}}), encoding="utf-8")
    return sidecar


def _make_jpg(path: Path) -> Path:
    img = Image.new("RGB", (8, 8), color=(200, 30, 30))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG")
    return path


def test_find_sidecar_patterns(tmp_path):
    media = tmp_path / "IMG_0001.JPG"
    media.write_bytes(b"x")
    sidecar = media.with_name("IMG_0001.JPG.json")
    sidecar.write_text("{}", encoding="utf-8")
    assert find_takeout_sidecar_json(media) == sidecar


def test_find_supplemental_metadata(tmp_path):
    media = tmp_path / "a.png"
    media.write_bytes(b"x")
    sidecar = media.with_name("a.png.supplemental-metadata.json")
    sidecar.write_text("{}", encoding="utf-8")
    assert find_takeout_sidecar_json(media) == sidecar


def test_get_capture_datetime_from_sidecar(tmp_path):
    media = tmp_path / "p.jpg"
    media.write_bytes(b"x")
    _write_sidecar(media, 1600000000)
    dt = get_capture_datetime(media, ["takeout_json", "mtime"], "utc")
    assert dt.year == 2020


def test_get_capture_year_sidecar_priority(tmp_path):
    media = tmp_path / "p.jpg"
    media.write_bytes(b"x")
    _write_sidecar(media, 1600000000)
    year = get_capture_year(media, ["takeout_json", "mtime"], "_UNKNOWN", "utc")
    assert year == "2020"


def test_sidecar_priority_over_mtime(tmp_path):
    media = tmp_path / "p.jpg"
    media.write_bytes(b"x")
    os.utime(media, (86400, 86400))
    _write_sidecar(media, 1700000000)
    assert get_date_source_used(media, ["takeout_json", "mtime"], "utc") == "takeout_json"
    year = get_capture_year(media, ["takeout_json", "mtime"], "UNKNOWN", "utc")
    assert year == "2023"


def test_mtime_fallback(tmp_path):
    media = tmp_path / "p.jpg"
    media.write_bytes(b"x")
    os.utime(media, (1600000000, 1600000000))
    src = get_date_source_used(media, ["takeout_json", "mtime"], "utc")
    assert src == "mtime"


def test_unknown_year_when_no_source(tmp_path):
    media = tmp_path / "p.jpg"
    media.write_bytes(b"x")
    year = get_capture_year(media, ["takeout_json"], "SIN_AÑO", "utc")
    assert year == "SIN_AÑO"


def test_exif_alone_may_be_unknown(tmp_path):
    media = tmp_path / "p.jpg"
    media.write_bytes(b"x")
    os.utime(media, (1, 1))
    year = get_capture_year(media, ["exif"], "SIN_AÑO", "utc")
    assert year == "SIN_AÑO"


def test_year_for_group_uses_winner(tmp_path):
    wins_f = tmp_path / "win.jpg"
    wins_f.write_bytes(b"x")
    os.utime(wins_f, (1290000000, 1290000000))
    _write_sidecar(wins_f, 1700000000)
    group = type("G", (), {"winner": str(wins_f)})()
    assert get_capture_year_for_group(group, ["takeout_json", "mtime"], "UNKNOWN", "utc") == "2023"


def test_year_for_group_accepts_path(tmp_path):
    f = tmp_path / "p.jpg"
    f.write_bytes(b"x")
    _write_sidecar(f, 1700000000)
    assert get_capture_year_for_group(f, ["takeout_json"], "UNKNOWN", "utc") == "2023"


def test_exif_datetime(tmp_path):
    jpg = tmp_path / "exif.jpg"
    img = Image.new("RGB", (8, 8), (10, 200, 10))
    exif = Image.Exif()
    exif[306] = b"2019:03:15 12:30:00"
    img.save(jpg, exif=exif, format="JPEG")
    year = get_capture_year(jpg, ["exif", "mtime"], "UNKNOWN", "utc")
    assert year == "2019"


def test_infer_account(tmp_path):
    root_a = tmp_path / "cuenta_a@gmail.com"
    root_b = tmp_path / "cuenta_b@gmail.com"
    root_a.mkdir()
    root_b.mkdir()
    f_a = root_a / "foto.jpg"
    f_b = root_b / "foto.jpg"
    f_a.write_bytes(b"a")
    f_b.write_bytes(b"b")
    inputs = (str(root_a), str(root_b))
    assert infer_account(str(f_a), inputs) == "cuenta_a@gmail.com"
    assert infer_account(str(f_b), inputs) == "cuenta_b@gmail.com"


def test_infer_account_unmatched(tmp_path):
    other = tmp_path / "otro" / "x.jpg"
    other.parent.mkdir()
    other.write_bytes(b"x")
    assert infer_account(str(other), ()) == ""