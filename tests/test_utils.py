"""Tests para utilidades de archivos."""

import hashlib
from pathlib import Path

from photos_dedupe.utils import (
    calculate_sha256,
    is_supported_image,
    is_supported_video,
    is_supported_media,
    safe_copy,
    safe_move,
    sanitize_path,
)


def test_calculate_sha256_known(tmp_path):
    data = b"hola mundo"
    p = tmp_path / "f.bin"
    p.write_bytes(data)
    assert calculate_sha256(str(p)) == hashlib.sha256(data).hexdigest()


def test_is_supported_image():
    assert is_supported_image("foto.JPG")
    assert is_supported_image("foto.heic")
    assert not is_supported_image("video.mp4")
    assert not is_supported_image("foto.json")


def test_is_supported_video():
    assert is_supported_video("clip.MP4")
    assert is_supported_video("clip.mkv")
    assert not is_supported_video("foto.png")


def test_is_supported_media():
    assert is_supported_media("a.jpg")
    assert is_supported_media("a.mp4")
    assert not is_supported_media("a.json")
    assert not is_supported_media("a.txt")


def test_safe_copy_flat(tmp_path):
    dst = tmp_path / "salida"
    src = tmp_path / "origen.txt"
    src.write_text("contenido", encoding="utf-8")
    out = safe_copy(str(src), str(dst))
    assert Path(out) == dst / "origen.txt"
    assert Path(out).read_text(encoding="utf-8") == "contenido"


def test_safe_copy_collision_appends_hash(tmp_path):
    dst = tmp_path / "salida"
    src_a = tmp_path / "a" / "dato.txt"
    src_b = tmp_path / "b" / "dato.txt"
    src_a.parent.mkdir()
    src_b.parent.mkdir()
    src_a.write_text("AAA", encoding="utf-8")
    src_b.write_text("BBB", encoding="utf-8")

    out1 = safe_copy(str(src_a), str(dst))
    out2 = safe_copy(str(src_b), str(dst))

    assert Path(out1).name == "dato.txt"
    h = hashlib.sha256(b"BBB").hexdigest()[:8]
    assert Path(out2).name == f"dato__{h}.txt"
    assert Path(out2).read_text(encoding="utf-8") == "BBB"


def test_safe_copy_creates_dirs(tmp_path):
    dst = tmp_path / "a" / "b" / "c"
    src = tmp_path / "x.txt"
    src.write_text("x", encoding="utf-8")
    out = safe_copy(str(src), str(dst))
    assert Path(out).exists()


def test_safe_move(tmp_path):
    dst = tmp_path / "salida"
    src = tmp_path / "m.txt"
    src.write_text("mover", encoding="utf-8")
    out = safe_move(str(src), str(dst))
    assert Path(out).exists()
    assert not src.exists()