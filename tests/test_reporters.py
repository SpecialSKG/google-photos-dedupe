"""Tests para reporters (Fases 7-9): auditoría por archivo, espacio real, estados."""

import json
from pathlib import Path

from PIL import Image

from photos_dedupe.config import Config
from photos_dedupe.dedupe import DuplicateGroup
from photos_dedupe.planner import build_plan
from photos_dedupe.reporters import Reporter


def _make_image(path: Path, size=(16, 16), color=(100, 100, 100)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def _sidecar(path: Path, timestamp: str = "1664654748") -> Path:
    sc = path.with_name(path.name + ".supplemental-metadata.json")
    sc.write_text(
        json.dumps({"photoTakenTime": {"timestamp": timestamp}}), encoding="utf-8"
    )
    return sc


class _FakeScanner:
    def __init__(self):
        self.file_roots = {}


def _cfg(tmp_path, exports) -> Config:
    cfg = Config()
    cfg.inputs = [str(exports)]
    cfg.action = "dry-run"
    cfg.timezone_mode = "utc"
    cfg.group_by_year = True
    return cfg


def _plan(tmp_path):
    exports = tmp_path / "exports"
    u = _make_image(exports / "u.jpg")
    _sidecar(u)
    e1 = _make_image(exports / "e1" / "d.jpg")
    e2 = _make_image(exports / "e2" / "d.jpg")
    e2.write_bytes(e1.read_bytes())
    _sidecar(e2)
    g = DuplicateGroup(1, "exact")
    g.winner = str(e1)
    g.duplicates = [str(e2)]
    files = [str(u), str(e1), str(e2)]
    return build_plan(files, [g], _cfg(tmp_path, exports), _FakeScanner())


def test_all_files_audit_covers_every_file(tmp_path):
    plan = _plan(tmp_path)
    out = tmp_path / "out"
    reporter = Reporter(str(out))
    path = reporter.generate_audit(plan)

    import csv
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3  # todos los archivos, no solo duplicados
    by_path = {r["source_path"]: r for r in rows}
    u = [r for r in rows if r["classification"] == "unique"]
    dup = [r for r in rows if r["classification"] == "exact_duplicate"]
    assert len(u) == 1
    assert len(dup) == 1
    assert dup[0]["output_bucket"] == "DUPLICATES_EXACT"
    assert u[0]["output_year"] == "2022"


def test_summary_with_plan_has_real_space_and_statuses(tmp_path):
    plan = _plan(tmp_path)
    out = tmp_path / "out"
    reporter = Reporter(str(out))
    reporter.generate_summary(
        total_files=3, unique_files=2, duplicate_groups=1, total_duplicates=1,
        detected_roots=["/fake"], mode="exact", action="dry-run", plan=plan,
    )
    txt = (out / "REPORTS" / "run_summary.txt").read_text(encoding="utf-8")
    assert "ESPACIO RECUPERABLE" in txt
    assert "630.00 B" in txt  # tamaño real de la copia exacta duplicada
    assert "ESTADOS DE EJECUCIÓN" in txt
    assert "planned: 3" in txt


def test_summary_without_plan_stays_compatible(tmp_path):
    out = tmp_path / "out"
    reporter = Reporter(str(out))
    reporter.generate_summary(
        total_files=1, unique_files=1, duplicate_groups=0, total_duplicates=0,
        detected_roots=[], mode="exact", action="dry-run",
    )
    txt = (out / "REPORTS" / "run_summary.txt").read_text(encoding="utf-8")
    assert "Space that can be saved: 0 bytes" in txt
