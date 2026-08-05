"""Tests para el planner (Fase 5): plan inmutable de procesamiento."""

import json
from pathlib import Path

from PIL import Image

from photos_dedupe.config import Config
from photos_dedupe.dedupe import DuplicateGroup
from photos_dedupe.planner import (
    BUCKET_DUPLICATES_EXACT,
    BUCKET_REVIEW_DATE,
    BUCKET_REVIEW_PERCEPTUAL,
    BUCKET_UNIQUE,
    build_plan,
    format_bytes,
    write_manifests,
)

# 2022-10-01T16:05:48Z
TS_2022 = "1664654748"
# 2021-11-01T14:00:00Z
TS_2021 = "1635760800"


def _make_image(path: Path, size=(16, 16), color=(100, 100, 100)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, format="JPEG")
    return path


def _sidecar(path: Path, timestamp: str = TS_2022) -> Path:
    sc = path.with_name(path.name + ".supplemental-metadata.json")
    sc.write_text(
        json.dumps({"photoTakenTime": {"timestamp": timestamp}}), encoding="utf-8"
    )
    return sc


class _FakeScanner:
    def __init__(self, file_roots=None):
        self.file_roots = file_roots or {}


def _config(tmp_path, exports, **overrides) -> Config:
    cfg = Config()
    cfg.inputs = [str(exports)]
    cfg.action = "dry-run"
    cfg.keep_structure = False
    cfg.timezone_mode = "utc"
    cfg.group_by_year = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _op_by_path(plan, source: str):
    return {op.source_path: op for op in plan.operations}[source]


# ---------- archivos únicos ----------

def test_unique_file_to_unique_bucket(tmp_path):
    exports = tmp_path / "exports"
    f = _make_image(exports / "foto.jpg")
    _sidecar(f)
    plan = build_plan([str(f)], [], _config(tmp_path, exports), _FakeScanner())

    assert len(plan.operations) == 1
    op = plan.operations[0]
    assert op.classification == "unique"
    assert op.output_bucket == BUCKET_UNIQUE
    assert op.output_year == "2022"
    assert op.is_recommended_winner
    assert not op.requires_review
    assert op.selected_date_source == "takeout_photo_taken_time"
    assert op.individual_date == op.selected_date
    assert plan.invariant_violations == []


def test_unique_file_without_date_to_review_date(tmp_path):
    exports = tmp_path / "exports"
    f = _make_image(exports / "pic.jpg")
    cfg = _config(tmp_path, exports, allow_filename_date=False)
    plan = build_plan([str(f)], [], cfg, _FakeScanner())

    op = plan.operations[0]
    assert op.output_bucket == BUCKET_REVIEW_DATE
    assert op.requires_review


# ---------- grupos exactos (caso WA0045) ----------

def test_exact_group_wa0045_canonical_year_and_winner_swap(tmp_path):
    exports = tmp_path / "exports"
    # Copia A: sin sidecar, solo mtime 2026 (fecha del export)
    a = _make_image(exports / "Fotos del 2022" / "IMG-20221001-WA0045.jpg")
    # Copia B: con supplemental-metadata.json → photoTakenTime 2022
    b = _make_image(exports / "Nuestros recuerdos(5)" / "IMG-20221001-WA0045.jpg")
    b.write_bytes(a.read_bytes())  # byte-idénticas (mismo SHA-256)
    _sidecar(b, TS_2022)

    g = DuplicateGroup(1, "exact")
    g.winner = str(a)  # lo que elegía el select_winner viejo (orden alfabético)
    g.duplicates = [str(b)]

    plan = build_plan([str(a), str(b)], [g], _config(tmp_path, exports), _FakeScanner())
    op_a = _op_by_path(plan, str(a))
    op_b = _op_by_path(plan, str(b))

    # Fecha canónica del grupo: 2022 (evidencia prestada de la copia B)
    assert op_a.output_year == "2022"
    assert op_b.output_year == "2022"
    assert op_a.canonical_group_date == op_b.canonical_group_date
    assert op_a.canonical_group_date.startswith("2022-")
    assert g.canonical_date_source == "takeout_photo_taken_time"
    assert g.canonical_date_source == op_a.selected_date_source

    # Winner real: la copia con sidecar (evidencia), no la alfabética sin fecha
    assert g.winner == str(b)
    assert op_b.classification == "exact_winner"
    assert op_b.output_bucket == BUCKET_UNIQUE
    assert op_a.classification == "exact_duplicate"
    assert op_a.output_bucket == BUCKET_DUPLICATES_EXACT
    assert plan.invariant_violations == []


def test_exact_group_no_evidence_goes_review(tmp_path):
    exports = tmp_path / "exports"
    a = _make_image(exports / "a.jpg")
    b = _make_image(exports / "b.jpg")
    b.write_bytes(a.read_bytes())

    g = DuplicateGroup(2, "exact")
    g.winner = str(a)
    g.duplicates = [str(b)]

    cfg = _config(tmp_path, exports, allow_filename_date=False, allow_mtime_as_capture_date=False)
    plan = build_plan([str(a), str(b)], [g], cfg, _FakeScanner())

    op_a = _op_by_path(plan, str(a))
    assert op_a.output_bucket == BUCKET_REVIEW_DATE
    assert op_a.requires_review


# ---------- grupos perceptuales ----------

def test_perceptual_review_all_keeps_all_members(tmp_path):
    exports = tmp_path / "exports"
    a = _make_image(exports / "a.jpg", (16, 16), (1, 2, 3))
    b = _make_image(exports / "b.jpg", (32, 32), (4, 5, 6))
    _sidecar(a, TS_2022)
    _sidecar(b, TS_2021)

    g = DuplicateGroup(10, "perceptual")
    g.winner = str(a)
    g.duplicates = [str(b)]
    g.phash_distance = 4

    cfg = _config(tmp_path, exports, perceptual_policy="review_all")
    plan = build_plan([str(a), str(b)], [g], cfg, _FakeScanner())

    op_a = _op_by_path(plan, str(a))
    op_b = _op_by_path(plan, str(b))
    assert op_a.output_bucket == BUCKET_REVIEW_PERCEPTUAL
    assert op_b.output_bucket == BUCKET_REVIEW_PERCEPTUAL
    assert op_a.requires_review and op_b.requires_review
    # Fechas individuales por miembro: NO se propagan del winner
    assert op_a.selected_date.startswith("2022-")
    assert op_b.selected_date.startswith("2021-")
    assert op_a.individual_date == op_a.selected_date
    assert op_a.canonical_group_date is None
    # Destino agrupado bajo group_000010
    assert "group_000010" in op_a.planned_destination
    assert "group_000010" in op_b.planned_destination


def test_perceptual_legacy_winner(tmp_path):
    exports = tmp_path / "exports"
    a = _make_image(exports / "a.jpg")
    b = _make_image(exports / "b.jpg", (32, 32), (9, 9, 9))
    _sidecar(a)
    _sidecar(b)

    g = DuplicateGroup(11, "perceptual")
    g.winner = str(a)
    g.duplicates = [str(b)]

    cfg = _config(tmp_path, exports, perceptual_policy="legacy_winner")
    plan = build_plan([str(a), str(b)], [g], cfg, _FakeScanner())

    op_a = _op_by_path(plan, str(a))
    op_b = _op_by_path(plan, str(b))
    assert op_a.output_bucket == BUCKET_UNIQUE
    assert op_b.output_bucket == BUCKET_DUPLICATES_EXACT
    assert not op_a.requires_review


# ---------- destinos y colisiones ----------

def test_collision_hash_suffix(tmp_path):
    exports = tmp_path / "exports"
    a = _make_image(exports / "d1" / "pic.jpg")
    b = _make_image(exports / "d2" / "pic.jpg")
    _sidecar(a)
    _sidecar(b)

    plan = build_plan([str(a), str(b)], [], _config(tmp_path, exports), _FakeScanner())
    op_a = _op_by_path(plan, str(a))
    op_b = _op_by_path(plan, str(b))

    assert op_a.collision_strategy == "unique"
    assert op_b.collision_strategy == "hash_suffix"
    assert "__" in Path(op_b.planned_destination).stem
    assert op_b.planned_destination != op_a.planned_destination


def test_keep_structure_nesting(tmp_path):
    exports = tmp_path / "exports"
    f = _make_image(exports / "Albums" / "Vacaciones" / "foto.jpg")
    _sidecar(f)

    cfg = _config(tmp_path, exports, keep_structure=True)
    file_roots = {str(f): str(exports)}
    plan = build_plan([str(f)], [], cfg, _FakeScanner(file_roots))

    op = plan.operations[0]
    assert op.relative_path == str(Path("Albums") / "Vacaciones" / "foto.jpg")
    expected = str(Path("UNIQUE") / "2022" / "Albums" / "Vacaciones" / "foto.jpg")
    assert op.planned_destination == expected


# ---------- metadatos planificados (Fase 6) ----------

def test_metadata_action_audit_with_high_confidence(tmp_path):
    exports = tmp_path / "exports"
    f = _make_image(exports / "foto.jpg")
    _sidecar(f)

    cfg = _config(tmp_path, exports, metadata_mode="audit")
    plan = build_plan([str(f)], [], cfg, _FakeScanner())
    assert plan.operations[0].metadata_action == "audit"

    cfg2 = _config(tmp_path, exports, metadata_mode="disabled")
    plan2 = build_plan([str(f)], [], cfg2, _FakeScanner())
    assert plan2.operations[0].metadata_action == "none"


def test_metadata_action_none_when_low_confidence(tmp_path):
    exports = tmp_path / "exports"
    # Fecha solo en el nombre del archivo → requiere revisión → sin metadata
    f = _make_image(exports / "IMG_20221001_160548.jpg")

    cfg = _config(
        tmp_path, exports, metadata_mode="audit",
        date_source_priority=["takeout_json", "exif", "filename"],
    )
    plan = build_plan([str(f)], [], cfg, _FakeScanner())

    op = plan.operations[0]
    assert op.selected_date_source == "filename"
    assert op.requires_review
    assert op.metadata_action == "none"


# ---------- invariantes y stats ----------

def test_plan_invariants_and_stats(tmp_path):
    exports = tmp_path / "exports"
    u = _make_image(exports / "unique.jpg")
    _sidecar(u)
    e1 = _make_image(exports / "e1" / "dup.jpg")
    e2 = _make_image(exports / "e2" / "dup.jpg")
    e2.write_bytes(e1.read_bytes())
    _sidecar(e2)
    p1 = _make_image(exports / "p1.jpg", (16, 16), (1, 1, 1))
    p2 = _make_image(exports / "p2.jpg", (32, 32), (2, 2, 2))
    _sidecar(p1)
    _sidecar(p2)

    g_exact = DuplicateGroup(1, "exact")
    g_exact.winner = str(e1)
    g_exact.duplicates = [str(e2)]
    g_perc = DuplicateGroup(2, "perceptual")
    g_perc.winner = str(p1)
    g_perc.duplicates = [str(p2)]

    cfg = _config(tmp_path, exports, perceptual_policy="review_all")
    files = [str(u), str(e1), str(e2), str(p1), str(p2)]
    plan = build_plan(files, [g_exact, g_perc], cfg, _FakeScanner())

    assert plan.invariant_violations == []
    assert plan.summary["total"] == 5
    counts = plan.summary["counts"]
    assert counts[BUCKET_UNIQUE] == 2          # unique + exact winner
    assert counts[BUCKET_DUPLICATES_EXACT] == 1
    assert counts[BUCKET_REVIEW_PERCEPTUAL] == 2
    # ahorro exacto garantizado = tamaño de la copia exacta
    assert plan.summary["guaranteed_exact_savings_bytes"] > 0


# ---------- helpers ----------

def test_format_bytes():
    assert format_bytes(0) == "0.00 B"
    assert format_bytes(1024) == "1.00 KiB"
    assert format_bytes(1536) == "1.50 KiB"
    assert format_bytes(1024**3) == "1.00 GiB"
    assert format_bytes(-1) == "0 B"
    assert format_bytes(None) == "0 B"


def test_manifests_serialize_numpy_distance(tmp_path):
    # Regresión: phash_distance provenía de numpy (np.int64) y rompía el manifest.
    import json
    import numpy as np
    exports = tmp_path / "exports"
    a = _make_image(exports / "a.jpg", (16, 16), (1, 2, 3))
    b = _make_image(exports / "b.jpg", (32, 32), (4, 5, 6))
    _sidecar(a)
    _sidecar(b)
    g = DuplicateGroup(5, "perceptual")
    g.winner = str(a)
    g.duplicates = [str(b)]
    g.phash_distance = np.int64(16)  # el tipo que rompía el json.dumps

    plan = build_plan([str(a), str(b)], [g], _config(tmp_path, exports), _FakeScanner())
    manifests = tmp_path / "manifests"
    write_manifests(plan, manifests, _config(tmp_path, exports), tmp_path)
    lines = [json.loads(l) for l in (manifests / "processing_plan.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    dists = [o["phash_distance"] for o in lines if o["phash_distance"] is not None]
    assert dists == [16, 16]  # ambos miembros del grupo perceptual
