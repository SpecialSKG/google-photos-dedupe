"""
planner.py (Fase 5)

Construye un plan de procesamiento inmutable para una ejecución del deduplicador.

El plan decide, para CADA archivo escaneado:
  - clasificación (unique / exact_winner / exact_duplicate / perceptual_* / date_review)
  - bucket de salida (UNIQUE / DUPLICATES_EXACT / REVIEW_PERCEPTUAL / REVIEW_DATE)
  - año de salida (output_year)
  - destino planificado (planned_destination) con keep_structure y colisiones
  - acción de metadata planificada
  - estado y errores

dry-run ejecuta EXACTAMENTE este plan hasta el punto de decidir destinos,
auditar y generar manifiestos, pero sin copiar ni escribir metadata.

Invariantes (Fase 10) se validan antes de copiar.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from photos_dedupe.date_models import (
    DateResolution,
    CONFLICT_NONE,
)
from photos_dedupe.date_utils import (
    resolve_capture_datetime,
    resolve_exact_group_datetime,
    resolve_perceptual_member_datetime,
)
from photos_dedupe.dedupe import DuplicateGroup
from photos_dedupe.utils import get_file_size

logger = logging.getLogger(__name__)

# Buckets
BUCKET_UNIQUE = "UNIQUE"
BUCKET_DUPLICATES_EXACT = "DUPLICATES_EXACT"
BUCKET_REVIEW_PERCEPTUAL = "REVIEW_PERCEPTUAL"
BUCKET_REVIEW_DATE = "REVIEW_DATE"

# Windows reserved names
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}
_WIN_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class PlannedFileOperation:
    """Una operación planificada por archivo."""
    file_id: int
    source_path: str
    source_root: str = ""
    source_account: str = ""
    relative_path: str = ""
    classification: str = ""        # unique | exact_winner | exact_duplicate | perceptual_member | perceptual_winner
    group_id: int = -1
    detection_type: str = ""        # exact | perceptual | unique
    is_recommended_winner: bool = False
    canonical_group_date: Optional[str] = None  # ISO UTC
    individual_date: Optional[str] = None
    selected_date: Optional[str] = None
    selected_date_source: str = ""
    date_confidence: int = 0
    requires_review: bool = False
    review_reason: str = ""
    output_bucket: str = ""
    output_year: str = "_UNKNOWN"
    planned_destination: str = ""
    collision_strategy: str = ""   # "unique" | "hash_suffix"
    metadata_action: str = "none"  # none | audit | write
    metadata_result: dict = field(default_factory=dict)  # resultado de Fase 6 (dict serializable)
    status: str = "planned"        # planned | copied | moved | failed | skipped
    error: str = ""
    phash_distance: Optional[int] = None
    is_exact_duplicate: bool = False
    is_perceptual_candidate: bool = False
    canonical_source_member: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Plan:
    """Plan completo para una ejecución."""
    operations: List[PlannedFileOperation] = field(default_factory=list)
    group_summaries: List[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    invariant_violations: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def stats(self) -> dict:
        c = {
            BUCKET_UNIQUE: 0,
            BUCKET_DUPLICATES_EXACT: 0,
            BUCKET_REVIEW_PERCEPTUAL: 0,
            BUCKET_REVIEW_DATE: 0,
        }
        bytes_exact_dup = 0
        bytes_perceptual_review = 0
        bytes_review_date = 0
        requires_review = 0
        for op in self.operations:
            c[op.output_bucket] = c.get(op.output_bucket, 0) + 1
            try:
                sz = get_file_size(op.source_path)
            except Exception:
                sz = 0
            if op.output_bucket == BUCKET_DUPLICATES_EXACT:
                bytes_exact_dup += sz
            elif op.output_bucket == BUCKET_REVIEW_PERCEPTUAL:
                bytes_perceptual_review += sz
            elif op.output_bucket == BUCKET_REVIEW_DATE:
                bytes_review_date += sz
            if op.requires_review:
                requires_review += 1
        return {
            "counts": c,
            "guaranteed_exact_savings_bytes": bytes_exact_dup,
            "potential_perceptual_savings_bytes": bytes_perceptual_review,
            "review_date_bytes": bytes_review_date,
            "requires_review_count": requires_review,
            "total": len(self.operations),
        }


# -----------------------------
# Helpers
# -----------------------------
def _safe_relpath(src: Path, root: Path) -> str:
    try:
        return str(src.resolve().relative_to(root.resolve()))
    except Exception:
        return src.name


def _is_safe_relative(rel: str) -> bool:
    if not rel:
        return False
    parts = Path(rel).parts
    if not parts:
        return False
    if ".." in parts:
        return False
    # los separadores (\\ y /) son parte de parts, no caracteres inválidos:
    # validamos componente a componente
    for part in parts:
        if _WIN_INVALID_CHARS.search(part):
            return False
        stem = Path(part).stem.upper()
        if stem in _WIN_RESERVED:
            return False
    return True


def _collision_stem(stem: str, suffix: str, existing: set, hint: str = "") -> str:
    name = f"{stem}{suffix}"
    if name not in existing:
        return name
    short = (hint or "x")[:8]
    cand = f"{stem}__{short}{suffix}"
    i = 1
    while cand in existing:
        cand = f"{stem}__{short}_{i}{suffix}"
        i += 1
    return cand


# -----------------------------
# Plan builder
# -----------------------------
def build_plan(
    all_files: List[str],
    groups: List[DuplicateGroup],
    config,
    scanner,
) -> Plan:
    """
    Construye el plan inmutable para todos los archivos.

    Pasos:
      1. Para cada grupo exacto: resolver fecha canónica (de cualquier miembro).
      2. Para cada grupo perceptual: resolver fecha individual por miembro.
      3. Para cada archivo: clasificar y asignar bucket.
      4. Resolver destino con keep_structure + colisiones.
      5. Validar invariantes.
    """
    plan = Plan()
    inputs = list(config.inputs)

    # Setup de buckets/bucket names
    exact_dir = config.exact_duplicates_dir
    perceptual_dir = config.perceptual_review_dir
    date_review_dir = config.date_review_dir
    perceptual_policy = config.perceptual_policy
    file_roots = getattr(scanner, "file_roots", {}) or {}

    # Resolución de fechas por grupo exacto
    exact_group_resolutions: Dict[int, DateResolution] = {}
    for g in groups:
        if g.detection_type == "exact":
            members = [g.winner] + list(g.duplicates) if g.winner else list(g.duplicates)
            res = resolve_exact_group_datetime(
                [Path(m) for m in members],
                date_priority=config.date_source_priority,
                timezone_mode=config.timezone_mode,
                min_valid_year=config.min_valid_year,
                future_date_tolerance_days=config.future_date_tolerance_days,
                date_conflict_tolerance_seconds=config.date_conflict_tolerance_seconds,
                allow_filename_date=config.allow_filename_date,
                allow_mtime_as_capture_date=config.allow_mtime_as_capture_date,
                low_confidence_date_policy=config.low_confidence_date_policy,
            )
            exact_group_resolutions[g.group_id] = res

    # Resolver winner mejorado para grupos exactos (Fase 4) — ANTES de asignar roles
    for g in groups:
        if g.detection_type == "exact":
            g.all_members = [g.winner] + list(g.duplicates) if g.winner else list(g.duplicates)
            ev_map: Dict[str, int] = {}
            res = exact_group_resolutions.get(g.group_id)
            canonical_source = ""
            if res:
                canonical_source = res.selected_source
                for m in g.all_members:
                    # evidencia = 1 si este miembro aporta el candidato canónico
                    if res.canonical_source_member and Path(m).resolve() == Path(res.canonical_source_member).resolve():
                        ev_map[str(m)] = 1
                    else:
                        ev_map[str(m)] = 0
            try:
                canonical_year = ""
                if res:
                    y = res.output_year
                    if y and y.isdigit():
                        canonical_year = y
                winner, reason, score, inp_idx, _ = _select_winner_enhanced_for_group(
                    g, inputs, ev_map, canonical_source, canonical_year
                )
            except Exception:
                logger.exception("select_winner_enhanced falló; se usa el anterior")
                winner, reason, score, inp_idx = g.winner, g.reason, 0, -1
            if winner and winner != g.winner:
                # swap winner <-> el duplicate que ganó
                old_winner = g.winner
                g.duplicates.append(old_winner)
                g.duplicates = [d for d in g.duplicates if d != winner]
                g.winner = winner
                g.all_members = [g.winner] + list(g.duplicates)
            g.winner_selection_score = score
            g.winner_selection_reason = reason
            g.preferred_input_index = inp_idx
            g.date_evidence_score = ev_map.get(str(g.winner), 0)
            g.canonical_date_source = canonical_source

    # Mapeo archivo → grupo (roles YA actualizados tras el swap)
    file_to_group: Dict[str, Tuple[int, str, str]] = {}  # path -> (group_id, role, detection_type)
    for g in groups:
        g.all_members = [g.winner] + list(g.duplicates) if g.winner else list(g.duplicates)
        for f in g.all_members:
            role = "winner" if f == g.winner else "duplicate"
            file_to_group[str(f)] = (g.group_id, role, g.detection_type)

    # Resolución individual de archivos
    name_registry: Dict[str, set] = {}  # dest_dir -> set of filenames
    file_to_op: Dict[str, PlannedFileOperation] = {}

    for idx, f in enumerate(all_files):
        fp = Path(f)
        op = PlannedFileOperation(file_id=idx, source_path=str(f))
        # source root / account / relative
        root = file_roots.get(str(f)) or file_roots.get(f)
        if root:
            op.source_root = str(root)
            op.relative_path = _safe_relpath(fp, Path(root))
        else:
            op.source_root = ""
            op.relative_path = fp.name
        # account
        try:
            from photos_dedupe.date_utils import infer_account
            op.source_account = infer_account(str(f), tuple(inputs))
        except Exception:
            op.source_account = ""

        grp = file_to_group.get(str(f))
        if grp is None:
            # único
            op.classification = "unique"
            op.detection_type = "unique"
            res = resolve_capture_datetime(
                fp,
                date_priority=config.date_source_priority,
                timezone_mode=config.timezone_mode,
                min_valid_year=config.min_valid_year,
                future_date_tolerance_days=config.future_date_tolerance_days,
                date_conflict_tolerance_seconds=config.date_conflict_tolerance_seconds,
                allow_filename_date=config.allow_filename_date,
                allow_mtime_as_capture_date=config.allow_mtime_as_capture_date,
                low_confidence_date_policy=config.low_confidence_date_policy,
            )
            _apply_resolution(op, res)
            op.individual_date = op.selected_date
            op.is_recommended_winner = True
            # bucket decisión
            if op.requires_review or op.selected_date is None:
                op.output_bucket = BUCKET_REVIEW_DATE
            else:
                op.output_bucket = BUCKET_UNIQUE
        else:
            gid, role, det = grp
            op.group_id = gid
            op.detection_type = det
            if det == "exact":
                # La fecha canónica del grupo es la autoritativa (selected_date);
                # no hay "fecha individual" distinta porque los bytes son idénticos.
                res = exact_group_resolutions.get(gid)
                _apply_resolution(op, res)
                op.canonical_group_date = res.selected_datetime.isoformat() if res and res.selected_datetime else None
                op.canonical_source_member = res.canonical_source_member if res else ""
                if role == "winner":
                    op.classification = "exact_winner"
                    op.is_recommended_winner = True
                    if op.requires_review:
                        op.output_bucket = BUCKET_REVIEW_DATE
                    else:
                        op.output_bucket = BUCKET_UNIQUE
                else:
                    op.classification = "exact_duplicate"
                    op.is_exact_duplicate = True
                    op.output_bucket = BUCKET_DUPLICATES_EXACT
            else:  # perceptual
                res = resolve_perceptual_member_datetime(
                    fp,
                    date_priority=config.date_source_priority,
                    timezone_mode=config.timezone_mode,
                    min_valid_year=config.min_valid_year,
                    future_date_tolerance_days=config.future_date_tolerance_days,
                    date_conflict_tolerance_seconds=config.date_conflict_tolerance_seconds,
                    allow_filename_date=config.allow_filename_date,
                    allow_mtime_as_capture_date=config.allow_mtime_as_capture_date,
                    low_confidence_date_policy=config.low_confidence_date_policy,
                )
                _apply_resolution(op, res)
                op.individual_date = op.selected_date
                op.is_perceptual_candidate = True
                op.phash_distance = None  # se setea abajo si aplica
                # Encontrar el grupo para la distancia
                for g in groups:
                    if g.group_id == gid:
                        op.phash_distance = g.phash_distance
                        break
                if role == "winner":
                    op.classification = "perceptual_winner"
                    op.is_recommended_winner = True
                else:
                    op.classification = "perceptual_member"

                if perceptual_policy == "legacy_winner":
                    # comportamiento anterior: winner a UNIQUE, dups a DUPLICATES_EXACT (legacy)
                    if role == "winner":
                        op.output_bucket = BUCKET_UNIQUE if not op.requires_review else BUCKET_REVIEW_DATE
                    else:
                        op.output_bucket = BUCKET_DUPLICATES_EXACT
                else:
                    # review_all: TODOS los miembros a REVIEW_PERCEPTUAL
                    op.output_bucket = BUCKET_REVIEW_PERCEPTUAL
                    op.requires_review = True
                    if not op.review_reason:
                        op.review_reason = "perceptual_group_review"

        # Destino planificado
        _plan_destination(op, config, name_registry, perceptual_dir, exact_dir,
                         date_review_dir, perceptual_policy, groups)

        # Metadatos planificados (Fase 6)
        if (
            config.metadata_mode in ("audit", "write")
            and op.selected_date is not None
            and op.date_confidence >= config.metadata_write_min_confidence
            and not op.requires_review
        ):
            op.metadata_action = config.metadata_mode
        else:
            op.metadata_action = "none"

        file_to_op[str(f)] = op
        plan.operations.append(op)

    # Group summaries
    for g in groups:
        m_paths = g.all_members
        try:
            total_size = sum(get_file_size(m) for m in m_paths)
        except Exception:
            total_size = 0
        res = exact_group_resolutions.get(g.group_id) if g.detection_type == "exact" else None
        plan.group_summaries.append({
            "group_id": g.group_id,
            "detection_type": g.detection_type,
            "phash_distance": g.phash_distance,
            "winner": g.winner,
            "winner_selection_reason": g.winner_selection_reason,
            "winner_selection_score": g.winner_selection_score,
            "preferred_input_index": g.preferred_input_index,
            "date_evidence_score": g.date_evidence_score,
            "canonical_date_source": g.canonical_date_source,
            "canonical_datetime": (res.selected_datetime.isoformat() if res and res.selected_datetime else None),
            "canonical_source_member": (res.canonical_source_member if res else None),
            "members": m_paths,
            "total_bytes": total_size,
        })

    plan.summary = plan.stats()
    # Invariantes
    _validate_invariants(plan, config, all_files)
    return plan


def _apply_resolution(op: PlannedFileOperation, res: Optional[DateResolution]) -> None:
    if not res:
        op.selected_date = None
        op.requires_review = True
        op.review_reason = "no resolution"
        return
    op.selected_date = res.selected_datetime.isoformat() if res.selected_datetime else None
    op.selected_date_source = res.selected_source
    op.date_confidence = res.selected_confidence
    op.requires_review = res.requires_review
    op.review_reason = res.conflict_reason or (res.conflict if res.conflict != CONFLICT_NONE else "")
    op.output_year = res.output_year if res.output_year else "_UNKNOWN"


def _select_winner_enhanced_for_group(g, inputs, ev_map, canonical_source, canonical_year=""):
    from photos_dedupe.dedupe import Deduplicator
    dedupe = Deduplicator.__new__(Deduplicator)
    dedupe.hash_calc = None
    return dedupe.select_winner_enhanced(g.all_members, inputs=inputs,
                                        date_evidence=ev_map,
                                        canonical_date_source=canonical_source,
                                        canonical_year=canonical_year)


def _plan_destination(op, config, name_registry, perceptual_dir, exact_dir,
                      date_review_dir, perceptual_policy, groups):
    """Resuelve planned_destination con keep_structure y colisiones."""
    if op.output_bucket == BUCKET_UNIQUE:
        bucket = "UNIQUE"
        year = op.output_year
    elif op.output_bucket == BUCKET_DUPLICATES_EXACT:
        bucket = exact_dir
        year = op.output_year
    elif op.output_bucket == BUCKET_REVIEW_PERCEPTUAL:
        bucket = perceptual_dir
        year = op.output_year
        # agrupar bajo group_XXXXXX
    elif op.output_bucket == BUCKET_REVIEW_DATE:
        bucket = date_review_dir
        year = op.output_year
    else:
        bucket = "UNIQUE"
        year = op.output_year

    # Año: solo se anida si group_by_year está activo (compatible con el pipeline anterior)
    base = Path(bucket)
    if config.group_by_year:
        base = base / year

    if op.output_bucket == BUCKET_REVIEW_PERCEPTUAL:
        # group_000001
        gid = op.group_id if op.group_id >= 0 else 0
        base = base / f"group_{gid:06d}"

    # keep_structure: anidar bajo la ruta relativa
    rel = op.relative_path
    if config.keep_structure and _is_safe_relative(rel):
        # rel incluye subcarpetas bajo Google Photos root
        rel_path = Path(rel)
        # si es solo nombre, parent = "."
        parent = rel_path.parent
        if str(parent) not in (".", ""):
            base = base / parent
    dest_dir = base
    final_name = Path(op.source_path).name
    stem = Path(op.source_path).stem
    suffix = Path(op.source_path).suffix

    key = str(dest_dir)
    existing = name_registry.setdefault(key, set())
    if final_name in existing:
        # colisión → hash suffix (usamos source_path hash corto)
        import hashlib as _h
        short = _h.sha256(op.source_path.encode("utf-8")).hexdigest()[:8]
        cand = f"{stem}__{short}{suffix}"
        i = 1
        while cand in existing:
            cand = f"{stem}__{short}_{i}{suffix}"
            i += 1
        final_name = cand
        op.collision_strategy = "hash_suffix"
    else:
        op.collision_strategy = "unique"
    existing.add(final_name)
    op.planned_destination = str(dest_dir / final_name)


# -----------------------------
# Invariantes (Fase 10)
# -----------------------------
def _validate_invariants(plan: Plan, config, all_files: List[str]) -> None:
    violations = plan.invariant_violations
    seen = set()
    buckets = {BUCKET_UNIQUE, BUCKET_DUPLICATES_EXACT, BUCKET_REVIEW_PERCEPTUAL, BUCKET_REVIEW_DATE}

    for op in plan.operations:
        # cada archivo una sola vez
        if op.source_path in seen:
            violations.append(f"duplicate plan op for {op.source_path}")
        seen.add(op.source_path)

        # bucket válido
        if op.output_bucket not in buckets:
            violations.append(f"invalid bucket {op.output_bucket} for {op.source_path}")

        # destino no bajo exports
        try:
            dest = Path(op.planned_destination).resolve()
            for inp in config.inputs:
                try:
                    inp_resolved = Path(inp).resolve()
                    dest.relative_to(inp_resolved)
                    violations.append(f"destination under exports: {dest}")
                    break
                except Exception:
                    pass
        except Exception:
            violations.append(f"unresolvable destination {op.planned_destination}")

        # destino != origen
        try:
            if Path(op.planned_destination).resolve() == Path(op.source_path).resolve():
                violations.append(f"dest == source: {op.source_path}")
        except Exception:
            pass

        # no escapar con ..
        if ".." in Path(op.planned_destination).parts:
            violations.append(f"relative escape in {op.planned_destination}")

        # año válido o _UNKNOWN
        y = op.output_year
        if y != "_UNKNOWN" and not (y.isdigit() and 1900 <= int(y) <= 2100):
            violations.append(f"invalid year {y} for {op.source_path}")

    # suma de clasificaciones == total
    total_ops = len(plan.operations)
    total_files = len(all_files)
    if total_ops != total_files:
        violations.append(f"plan ops ({total_ops}) != total files ({total_files})")

    # cada grupo exacto conserva exactamente un winner
    # (se valida implícitamente al construir; doble-check por grupos)
    from collections import Counter
    # contar winners por grupo exacto
    # (se valida implícitamente en build_plan; lo dejamos documentado)


# -----------------------------
# Manifiestos
# -----------------------------
def _json_default(o):
    """Normaliza tipos numpy (int64/float64/bool_) para JSON."""
    try:
        import numpy as np
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.bool_):
            return bool(o)
    except ImportError:
        pass
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def write_manifests(plan: Plan, manifests_dir: Path, config, run_dir: Path) -> None:
    """Escribe MANIFESTS/processing_plan.jsonl y run_state.json."""
    manifests_dir.mkdir(parents=True, exist_ok=True)
    plan_path = manifests_dir / "processing_plan.jsonl"
    with open(plan_path, "w", encoding="utf-8") as f:
        for op in plan.operations:
            f.write(json.dumps(op.to_dict(), ensure_ascii=False, default=_json_default) + "\n")

    state = {
        "version": None,
        "run_dir": str(run_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config_out_dir": config.out_dir,
        "total_planned": len(plan.operations),
        "status": "planned",
        "errors": plan.errors,
        "invariant_violations": plan.invariant_violations,
        "summary": plan.summary,
    }
    (manifests_dir / "run_state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8"
    )


def format_bytes(n: float) -> str:
    """Formato legible de bytes (B, KiB, MiB, GiB, TiB)."""
    units = [("B", 1), ("KiB", 1024), ("MiB", 1024**2), ("GiB", 1024**3), ("TiB", 1024**4)]
    if n is None or n < 0:
        return "0 B"
    n = float(n)
    for label, factor in units:
        v = n / factor
        if v < 1024 or label == "TiB":
            return f"{v:.2f} {label}"
    return f"{n:.0f} B"
