"""Tests end-to-end del CLI mediante subprocess (fixture minimo de Takeout)."""

import os
import subprocess
import sys
import json
from pathlib import Path

from PIL import Image
import io


def _img(size=(16, 16), color=(10, 20, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _build_takeout(root: Path, *, spanish: bool = True):
    photos = "Google Fotos" if spanish else "Google Photos"
    a1 = root / "account1" / "Takeout" / photos / "viaje"
    a2 = root / "account2" / "Takeout" / photos / "viaje"
    a1.mkdir(parents=True)
    a2.mkdir(parents=True)

    shared = _img((24, 24), (123, 45, 200))
    (a1 / "f1.jpg").write_bytes(shared)
    (a2 / "f1.jpg").write_bytes(shared)
    (a1 / "f2.jpg").write_bytes(_img((64, 64), (10, 200, 30)))

    json.dump({"photoTakenTime": {"timestamp": "1609459200"}},
              open(a1 / "f1.jpg.json", "w"))


def _write_cfg(path: Path, root: Path, out: Path, **overrides):
    vals = dict(
        mode="exact+perceptual",
        action="dry-run",
        keep_structure=True,
        group_by_year=False,
        phash_threshold=6,
        workers=4,
        ignore_json=True,
        out_dir=str(out),
    )
    vals.update(overrides)
    lines = [
        "inputs:",
        f"  - {root / 'account1'}",
        f"  - {root / 'account2'}",
        f"out_dir: {vals['out_dir']}",
        f"mode: {vals['mode']}",
        f"action: {vals['action']}",
        f"phash_threshold: {vals['phash_threshold']}",
        f"workers: {vals['workers']}",
        f"keep_structure: {str(vals['keep_structure']).lower()}",
        f"ignore_json: {str(vals['ignore_json']).lower()}",
        f"group_by_year: {str(vals['group_by_year']).lower()}",
        "reports:",
        "  csv: true",
        "  json: true",
        "  xlsx: true",
    ]
    if vals.get("date_source_priority"):
        lines.append("date_source_priority:")
        for s in vals["date_source_priority"]:
            lines.append(f"  - {s}")
    if vals.get("metadata_mode"):
        lines.append(f"metadata_mode: {vals['metadata_mode']}")
    if vals.get("verify_written_metadata") is not None:
        lines.append(f"verify_written_metadata: {str(vals['verify_written_metadata']).lower()}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run(cfg: Path, *extra):
    return subprocess.run(
        [sys.executable, "-m", "photos_dedupe", "--config", str(cfg), *extra],
        capture_output=True, text=True,
    )


def _run_dir(out: Path) -> Path:
    runs = sorted((p for p in out.iterdir() if p.is_dir() and p.name.startswith("run_")))
    assert runs, f"no run_* dirs in {out}"
    return runs[-1]


def test_dry_run_passes(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="dry-run")
    r = _run(cfg, "--action", "dry-run")
    assert r.returncode == 0, r.stderr
    rd = _run_dir(out)
    assert (rd / "LOGS" / "run.log").exists()
    assert (rd / "REPORTS" / "dedupe_report.csv").exists()
    assert (out / "run.log").exists() is False  # no pisa la raíz


def test_dry_run_writes_plan_and_audit(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="dry-run")
    r = _run(cfg, "--action", "dry-run")
    assert r.returncode == 0, r.stderr
    rd = _run_dir(out)
    assert (rd / "MANIFESTS" / "processing_plan.jsonl").exists()
    assert (rd / "MANIFESTS" / "run_state.json").exists()
    assert (rd / "REPORTS" / "all_files_audit.csv").exists()
    # el resumen ya no dice "0 bytes" (espacio real)
    txt = (rd / "REPORTS" / "run_summary.txt").read_text(encoding="utf-8")
    assert "Space that can be saved: 0 bytes" not in txt
    assert "Ahorro exacto garantizado" in txt


def test_dry_run_review_warnings_exit_zero(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="dry-run")
    r = _run(cfg, "--action", "dry-run")
    assert r.returncode == 0, r.stderr
    log = ( _run_dir(out) / "LOGS" / "run.log").read_text(encoding="utf-8")
    assert "COMPLETED WITH WARNINGS" in log or "COMPLETED SUCCESSFULLY" in log


def test_move_requires_confirm(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="move")
    r = _run(cfg, "--action", "move")
    assert r.returncode == 2
    assert "ADVERTENCIA" in r.stderr


def test_move_confirmed_works(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="move")
    r = _run(cfg, "--action", "move", "--confirm-move")
    assert r.returncode == 0, r.stderr
    rd = _run_dir(out)
    assert (rd / "UNIQUE").exists()
    assert (rd / "DUPLICATES_EXACT").exists()


def test_keep_structure_mirrors_subdirs(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="copy", keep_structure=True, group_by_year=False)
    r = _run(cfg, "--action", "copy")
    assert r.returncode == 0, r.stderr
    rd = _run_dir(out)
    assert (rd / "UNIQUE" / "viaje" / "f1.jpg").exists()
    assert (rd / "DUPLICATES_EXACT" / "viaje" / "f1.jpg").exists()
    # f2 sin evidencia real → REVIEW_DATE (mantiene estructura viaje)
    assert (rd / "REVIEW_DATE" / "viaje" / "f2.jpg").exists()
    # exports intact (copy no destruye)
    assert (root / "account1" / "Takeout" / "Google Fotos" / "viaje" / "f1.jpg").exists()


def test_keep_structure_flat_when_false(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="copy", keep_structure=False, group_by_year=False)
    r = _run(cfg, "--action", "copy")
    assert r.returncode == 0, r.stderr
    rd = _run_dir(out)
    assert (rd / "UNIQUE" / "f1.jpg").exists()
    assert not (rd / "UNIQUE" / "viaje").exists()


def test_group_by_year_unique_uses_file_year(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="copy", keep_structure=True, group_by_year=True)
    r = _run(cfg, "--action", "copy")
    assert r.returncode == 0, r.stderr
    rd = _run_dir(out)
    names = {p.name for p in (rd / "UNIQUE").iterdir()}
    assert "2020" in names
    # f2 (único sin sidecar ni EXIF) queda en revisión por evidencia débil (mtime)
    assert (rd / "REVIEW_DATE").exists()
    assert not (rd / "UNIQUE" / "_UNKNOWN").exists()


def test_consecutive_runs_do_not_overlap(tmp_path):
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="dry-run")
    r1 = _run(cfg, "--action", "dry-run")
    assert r1.returncode == 0, r1.stderr
    r2 = _run(cfg, "--action", "dry-run")
    assert r2.returncode == 0, r2.stderr
    runs = [p for p in out.iterdir() if p.is_dir() and p.name.startswith("run_")]
    assert len(runs) == 2
    first, second = sorted(runs, key=lambda p: p.name)
    assert first != second
    assert (first / "REPORTS" / "run_summary.txt").exists()
    assert (second / "REPORTS" / "run_summary.txt").exists()


def test_hash_cache_persists_between_runs(tmp_path):
    """Fase 18: la caché de hashes sobrevive entre corridas y evita recalcular."""
    root = tmp_path / "takeout"
    out = tmp_path / "out"
    _build_takeout(root)
    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="dry-run")

    r1 = _run(cfg, "--action", "dry-run")
    assert r1.returncode == 0, r1.stderr
    cache = out / ".photos_dedupe.hash_cache.json"
    assert cache.exists(), "la primera corrida debe guardar la caché de hashes"

    r2 = _run(cfg, "--action", "dry-run")
    assert r2.returncode == 0, r2.stderr
    log2 = _run_dir(out) / "LOGS" / "run.log"
    log_text = log2.read_text(encoding="utf-8")
    assert "Caché de hashes cargada" in log_text
    assert "phash hits" in log_text  # la segunda corrida reutilizó hashes, no recalculó


def test_copy_with_metadata_write_sets_exif(tmp_path):
    """Fase 18: con ExifTool real, una corrida copy + metadata_mode=write escribe
    DateTimeOriginal en la copia (y la verificación pasa)."""
    et = None
    try:
        from photos_dedupe.config import Config
        from photos_dedupe.metadata_writer import exiftool_binary
        et = exiftool_binary(Config())
    except Exception:
        pass
    if not et:
        import pytest
        pytest.skip("ExifTool no instalado")

    root = tmp_path / "takeout"
    out = tmp_path / "out"
    photos = "Google Fotos"
    a1 = root / "account1" / "Takeout" / photos / "viaje"
    a1.mkdir(parents=True)
    a2 = root / "account2" / "Takeout" / photos
    a2.mkdir(parents=True)
    (a1 / "f2.jpg").write_bytes(_img((64, 64), (10, 200, 30)))
    # sidecar con timestamp → confianza alta (100) → metadata_action = write
    json.dump({"photoTakenTime": {"timestamp": "1609459200"}},
              open(a1 / "f2.jpg.json", "w"))

    cfg = tmp_path / "config.yaml"
    _write_cfg(cfg, root, out, action="copy", metadata_mode="write", verify_written_metadata=True)
    r = _run(cfg, "--action", "copy")
    assert r.returncode == 0, r.stderr

    from photos_dedupe.metadata_writer import _exif_datetime_original
    rd = _run_dir(out)
    copies = list(rd.rglob("f2.jpg"))
    assert copies, f"no se copió f2.jpg en {rd}"
    dt = _exif_datetime_original(copies[0])
    assert dt is not None, "la copia debe tener DateTimeOriginal escrito"
    # 1609459200 UTC = 2021-01-01 00:00 UTC → hora local (ej. UTC-3 → 2020-12-31 21:00)
    from datetime import datetime, timezone
    expected = datetime.fromisoformat("2021-01-01T00:00:00+00:00").astimezone().replace(tzinfo=None)
    assert dt.strftime("%Y:%m:%d %H:%M:%S") == expected.strftime("%Y:%m:%d %H:%M:%S")
