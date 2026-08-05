"""Tests para el deduplicador (exacto y perceptual)."""

import os
from pathlib import Path

import pytest
from PIL import Image

from photos_dedupe.dedupe import Deduplicator
from photos_dedupe.hashing import HashCalculator


def _make_image(path: Path, size=(16, 16), color=(100, 100, 100)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def _make_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---------- select_winner ----------

def test_winner_higher_resolution(tmp_path):
    small = _make_image(tmp_path / "small.jpg", (8, 8), (200, 0, 0))
    big = _make_image(tmp_path / "big.jpg", (32, 32), (0, 0, 200))
    dedupe = Deduplicator()
    winner, reason = dedupe.select_winner([str(big), str(small)])
    assert winner == str(big)
    assert "resolution" in reason


def test_winner_larger_size(tmp_path):
    a = _make_image(tmp_path / "a.jpg", (16, 16))
    b_path = _make_image(tmp_path / "b.jpg", (16, 16))
    b_path.write_bytes(b_path.read_bytes() + b"\x00" * 2048)
    dedupe = Deduplicator()
    winner, reason = dedupe.select_winner([str(a), str(b_path)])
    assert winner == str(b_path)
    assert "size" in reason


def test_winner_alphabetical_on_tie(tmp_path):
    a = _make_image(tmp_path / "a.jpg")
    b = _make_image(tmp_path / "b.jpg")
    b.write_bytes(a.read_bytes())
    dedupe = Deduplicator()
    winner, reason = dedupe.select_winner([str(b), str(a)])
    assert winner == str(a)
    assert "alphabetically" in reason


# ---------- exact ----------

def test_exact_duplicates(tmp_path):
    f1 = _make_file(tmp_path / "d1" / "x.jpg", b"SAME-BYTE-SAME")
    f2 = _make_file(tmp_path / "d2" / "x.jpg", b"SAME-BYTE-SAME")
    dedupe = Deduplicator()
    groups = dedupe.find_exact_duplicates([str(f1), str(f2)])
    assert len(groups) == 1
    assert set(groups[0]) == {str(f1), str(f2)}


def test_exact_no_duplicates(tmp_path):
    f1 = _make_file(tmp_path / "a.txt", b"AAA")
    f2 = _make_file(tmp_path / "b.txt", b"BBB")
    dedupe = Deduplicator()
    assert dedupe.find_exact_duplicates([str(f1), str(f2)]) == []


def test_create_groups_exact_members(tmp_path):
    f1 = _make_file(tmp_path / "d1" / "x.jpg", b"aaaa")
    f2 = _make_file(tmp_path / "d2" / "x.jpg", b"aaaa")
    f3 = _make_file(tmp_path / "d3" / "x.jpg", b"bbbb")
    dedupe = Deduplicator(mode="exact")
    groups = dedupe.create_duplicate_groups([str(f1), str(f2), str(f3)])
    assert len(groups) == 1
    assert groups[0].detection_type == "exact"
    winners = dedupe.get_all_winners()
    dupes = dedupe.get_all_duplicates()
    assert len(winners) == 1
    assert len(dupes) == 1
    assert dedupe.get_unique_files([str(f1), str(f2), str(f3)]) == [str(f3)]


# ---------- perceptual ----------

def test_hamming_distance():
    calc = HashCalculator()
    assert calc.hamming_distance("0" * 16, "0" * 16) == 0
    assert calc.hamming_distance("0" * 16, "8" * 16) == 16
    assert calc.hamming_distance("0" * 16, "F" * 16) == 64


def test_perceptual_identical_images(tmp_path):
    a = _make_image(tmp_path / "a.jpg")
    b = _make_image(tmp_path / "b.jpg")
    b.write_bytes(a.read_bytes())
    dedupe = Deduplicator(mode="perceptual", phash_threshold=6)
    groups = dedupe.create_duplicate_groups([str(a), str(b)])
    assert len(groups) == 1
    assert groups[0].detection_type == "perceptual"
    assert {groups[0].winner, *groups[0].duplicates} == {str(a), str(b)}


def test_perceptual_threshold_groups_by_distance(tmp_path):
    f0 = _make_image(tmp_path / "f0.jpg")
    f1 = _make_image(tmp_path / "f1.jpg")
    f2 = _make_image(tmp_path / "f2.jpg")
    fake = {str(f0): "0" * 16, str(f1): "0" * 16, str(f2): "F" * 16}
    dedupe = Deduplicator(mode="perceptual", phash_threshold=2)
    dedupe.hash_calc.get_phash = lambda p: fake[p]
    groups = dedupe.create_duplicate_groups([str(f0), str(f1), str(f2)])
    assert len(groups) == 1
    assert groups[0].detection_type == "perceptual"
    assert {groups[0].winner, *groups[0].duplicates} == {str(f0), str(f1)}


def test_perceptual_not_grouped_when_far(tmp_path):
    f0 = _make_image(tmp_path / "f0.jpg")
    f1 = _make_image(tmp_path / "f1.jpg")
    fake = {str(f0): "0" * 16, str(f1): "F" * 16}
    dedupe = Deduplicator(mode="perceptual", phash_threshold=2)
    dedupe.hash_calc.get_phash = lambda p: fake[p]
    groups = dedupe.create_duplicate_groups([str(f0), str(f1)])
    assert groups == []


def test_perceptual_in_exact_plus_exact_wins(tmp_path):
    a = _make_image(tmp_path / "a.jpg")
    b = _make_image(tmp_path / "b.jpg")
    b.write_bytes(a.read_bytes())
    c = _make_image(tmp_path / "c.jpg", (64, 64), (1, 2, 3))
    dedupe = Deduplicator(mode="exact+perceptual", phash_threshold=6)
    groups = dedupe.create_duplicate_groups([str(a), str(b), str(c)])
    assert len(groups) == 1
    assert groups[0].detection_type == "exact"


# ---------- select_winner_enhanced (Fase 4) ----------

def test_winner_enhanced_prefers_first_input(tmp_path):
    a = _make_image(tmp_path / "primary" / "x.jpg", (16, 16))
    b = _make_image(tmp_path / "secondary" / "x.jpg", (16, 16))
    b.write_bytes(a.read_bytes())
    dedupe = Deduplicator()
    winner, reason, score, inp_idx, _ = dedupe.select_winner_enhanced(
        [str(b), str(a)],
        inputs=[str(tmp_path / "primary"), str(tmp_path / "secondary")],
    )
    assert winner == str(a)
    assert "preferred input" in reason
    assert inp_idx == 0


def test_winner_enhanced_richer_sidecar_evidence(tmp_path):
    # Ambos archivos en el mismo input → gana el que tiene sidecar
    a = _make_image(tmp_path / "x" / "f.jpg", (16, 16))
    b = _make_image(tmp_path / "x" / "g.jpg", (16, 16))
    b.write_bytes(a.read_bytes())
    (tmp_path / "x" / "g.jpg.json").write_text('{"photoTakenTime": {"timestamp": "1664654748"}}')
    dedupe = Deduplicator()
    winner, reason, _, _, _ = dedupe.select_winner_enhanced(
        [str(a), str(b)],
        inputs=[str(tmp_path / "x")],
        date_evidence={str(a): 0, str(b): 1},
    )
    assert winner == str(b)
    assert "sidecar" in reason.lower()


def test_winner_enhanced_alphabetical_as_tiebreak(tmp_path):
    z = _make_image(tmp_path / "z.jpg")
    a = _make_image(tmp_path / "a.jpg")
    a.write_bytes(z.read_bytes())
    dedupe = Deduplicator()
    winner, reason, _, _, _ = dedupe.select_winner_enhanced([str(z), str(a)])
    assert winner == str(a)  # a.jpg gana alfabéticamente
    assert "alphabet" in reason.lower()