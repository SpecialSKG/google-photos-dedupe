"""Tests para la caché persistente de hashes (Fase 18)."""

import json
import os
from pathlib import Path

import pytest
from PIL import Image

import photos_dedupe.hashing as h
from photos_dedupe.hashing import HashCalculator


def _make_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_image(path: Path, size=(16, 16), color=(50, 50, 50)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def _boom(*args, **kwargs):
    raise AssertionError("recompute: se llamó al cálculo real")


# ---------- sha256 ----------

def test_sha256_cache_persist_and_load_avoids_recompute(tmp_path):
    f = _make_file(tmp_path / "a.bin", b"hola mundo")
    cf = tmp_path / "cache.json"

    c1 = HashCalculator(str(cf))
    s1 = c1.get_sha256(str(f))
    assert c1.save_cache() == 1

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("photos_dedupe.hashing.calculate_sha256", _boom)
    try:
        c2 = HashCalculator(str(cf))
        assert c2.get_sha256(str(f)) == s1
        assert c2.cache_stats()["sha256_hits"] == 1
    finally:
        monkeypatch.undo()


def test_cache_invalidated_when_content_changes(tmp_path):
    f = _make_file(tmp_path / "a.bin", b"version 1")
    cf = tmp_path / "cache.json"

    c1 = HashCalculator(str(cf))
    s1 = c1.get_sha256(str(f))
    c1.save_cache()

    f.write_bytes(b"version 2 ... mas largo")
    c2 = HashCalculator(str(cf))
    assert c2.get_sha256(str(f)) != s1
    assert c2.cache_stats()["sha256_hits"] == 0


def test_cache_invalidated_when_only_mtime_changes(tmp_path):
    f = _make_file(tmp_path / "a.bin", b"hola")
    cf = tmp_path / "cache.json"

    c1 = HashCalculator(str(cf))
    s1 = c1.get_sha256(str(f))
    c1.save_cache()

    new_mtime = os.path.getmtime(f) + 3600
    os.utime(f, (new_mtime, new_mtime))
    c2 = HashCalculator(str(cf))
    # El contenido no cambió (mismo hash), pero DEBE recomputarse: no es hit de caché.
    assert c2.cache_stats()["sha256_hits"] == 0
    assert c2.get_sha256(str(f)) == s1
    assert c2.cache_stats()["sha256_hits"] == 0


def test_cache_prunes_deleted_files_on_save(tmp_path):
    f = _make_file(tmp_path / "a.bin", b"hola")
    cf = tmp_path / "cache.json"

    c1 = HashCalculator(str(cf))
    c1.get_sha256(str(f))
    assert c1.save_cache() == 1

    f.unlink()
    c2 = HashCalculator(str(cf))
    assert c2.save_cache(str(cf)) == 0


def test_corrupt_cache_file_ignored(tmp_path):
    cf = tmp_path / "cache.json"
    cf.write_text("esto no es json {")
    c = HashCalculator(str(cf))
    assert c.load_cache(str(cf)) == 0
    # sigue funcionando y recalcula
    f = _make_file(tmp_path / "b.bin", b"x")
    assert c.get_sha256(str(f))


def test_wrong_version_cache_ignored(tmp_path):
    cf = tmp_path / "cache.json"
    cf.write_text(json.dumps({"version": 999, "entries": {"x": {"size": 1, "mtime_ns": 2, "sha256": "abc"}}}))
    c = HashCalculator(str(cf))
    assert c.load_cache(str(cf)) == 0


def test_clear_cache_clears_disk(tmp_path):
    f = _make_file(tmp_path / "a.bin", b"hola")
    cf = tmp_path / "cache.json"
    c1 = HashCalculator(str(cf))
    c1.get_sha256(str(f))
    c1.save_cache()
    c1.clear_cache()
    assert c1.save_cache() == 0


# ---------- phash ----------

def test_phash_persisted_and_avoids_decode(tmp_path, monkeypatch):
    img = _make_image(tmp_path / "foto.jpg")
    cf = tmp_path / "cache.json"

    c1 = HashCalculator(str(cf))
    p1 = c1.get_phash(str(img))
    assert p1
    c1.save_cache()

    class Boom:
        @classmethod
        def open(cls, *a, **k):
            raise AssertionError("recompute: no debe decodificar la imagen")

    monkeypatch.setattr(h, "Image", Boom)
    try:
        c2 = HashCalculator(str(cf))
        assert c2.get_phash(str(img)) == p1
        assert c2.cache_stats()["phash_hits"] == 1
    finally:
        monkeypatch.undo()


# ---------- e2e con el Deduplicator ----------

def test_deduplicator_second_run_uses_cache(tmp_path, monkeypatch):
    from photos_dedupe.dedupe import Deduplicator

    a = _make_image(tmp_path / "a.jpg")
    b = _make_image(tmp_path / "b.jpg")
    b.write_bytes(a.read_bytes())  # duplicado exacto
    c = _make_image(tmp_path / "c.jpg", (16, 16), (200, 10, 10))
    files = [str(a), str(b), str(c)]
    cf = tmp_path / "cache.json"

    d1 = Deduplicator(mode="exact+perceptual", workers=2, hash_cache_file=str(cf))
    groups1 = [(g.group_id, sorted([g.winner] + g.duplicates)) for g in d1.create_duplicate_groups(files)]
    d1.hash_calc.save_cache()

    # Segunda corrida: ningún hash real debe recalcularse (todo viene de la caché).
    monkeypatch.setattr(h, "Image", _BoomContext)
    monkeypatch.setattr(h, "calculate_sha256", _boom)
    try:
        d2 = Deduplicator(mode="exact+perceptual", workers=2, hash_cache_file=str(cf))
        groups2 = [(g.group_id, sorted([g.winner] + g.duplicates)) for g in d2.create_duplicate_groups(files)]
        assert sorted(g1[1] for g1 in groups1) == sorted(g2[1] for g2 in groups2)
        assert groups1 == groups2
    finally:
        monkeypatch.undo()


class _BoomContext:
    """Reemplaza a PIL.Image.open con un contexto que explota si se abre."""
    @classmethod
    def open(cls, *a, **k):
        raise AssertionError("no debe decodificarse la imagen (caché)")