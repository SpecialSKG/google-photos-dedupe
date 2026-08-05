"""
date_utils.py

Utilities for extracting capture dates (year) from:
- Google Takeout JSON sidecars (photoTakenTime vs creationTime separados)
- EXIF metadata
- QuickTime video metadata
- Filename patterns
- Folder year hints
- File modification time (mtime)

Also includes account inference helpers.

Core: resolve_capture_datetime() devuelve un DateResolution con evidencia completa.
Public wrappers (get_capture_datetime, get_capture_year, get_date_source_used,
get_capture_year_for_group) se mantienen compatibles con la API anterior.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Any

from photos_dedupe.date_models import (
    DateCandidate,
    DateResolution,
    DEFAULT_CONFIDENCE,
    SOURCE_PHOTO_TAKEN_TIME,
    SOURCE_CREATION_TIME,
    SOURCE_EXIF_DATETIME_ORIGINAL,
    SOURCE_EXIF_DATETIME_DIGITIZED,
    SOURCE_EXIF_DATETIME,
    SOURCE_QUICKTIME_MEDIA_CREATE,
    SOURCE_QUICKTIME_TRACK_CREATE,
    SOURCE_QUICKTIME_CREATE,
    SOURCE_FILENAME,
    SOURCE_FOLDER_YEAR_HINT,
    SOURCE_MTIME,
    CONFLICT_NONE,
    CONFLICT_NO_DATE_FOUND,
    CONFLICT_ONLY_MTIME_AVAILABLE,
    CONFLICT_SIDECAR_AMBIGUOUS,
    CONFLICT_HIGH_CONFIDENCE,
    CONFLICT_FUTURE_DATE_REJECTED,
    CONFLICT_INVALID_EXIF,
    CONFLICT_FILENAME_DATE_ONLY,
    CONFLICT_LOW_CONFIDENCE,
    CONFLICT_GROUP_DATE_BORROWED,
)

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ExifTags
except Exception:
    Image = None
    ExifTags = None


# -----------------------------
# Sidecar JSON helpers
# -----------------------------
_SIDECAR_PATTERNS = (
    "{name}.json",
    "{name}.supplemental-metadata.json",
    "{stem}.json",
    "{stem}.supplemental-metadata.json",
)


def _sidecar_candidates(media_path: Path) -> List[Path]:
    """Lista determinista de sidecars posibles (sin tocar el filesystem)."""
    return [
        media_path.with_name(p.format(name=media_path.name, stem=media_path.stem))
        for p in _SIDECAR_PATTERNS
    ]


def find_takeout_sidecar_json(media_path: Path) -> Optional[Path]:
    """
    Locate Google Takeout JSON sidecar for a given media file.
    Returns None if absent OR if multiple distinct sidecars exist (ambiguous).
    """
    if not media_path:
        return None

    found = []
    for c in _sidecar_candidates(media_path):
        try:
            if c.exists():
                found.append(c)
        except Exception:
            pass

    if len(found) > 1:
        # Varios candidatos distintos → ambigüedad. No elegimos aproximado.
        logger.warning(
            "Sidecar ambiguo para %s: %s", media_path, ", ".join(str(f) for f in found)
        )
        return None

    return found[0] if found else None


def find_takeout_sidecar_json_all(media_path: Path) -> List[Path]:
    """Devuelve TODOS los sidecars existentes (para auditar ambigüedad)."""
    if not media_path:
        return []
    return [c for c in _sidecar_candidates(media_path) if c.exists()]


# -----------------------------
# Takeout JSON timestamp
# -----------------------------
def _read_takeout_ts(json_path: Path, key: str) -> Optional[int]:
    """Lee el timestamp Unix (int o str) de una clave dada del JSON de Takeout."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        node = data.get(key)
        if not isinstance(node, dict):
            return None
        ts = node.get("timestamp")
        if ts is None:
            return None
        return int(ts)
    except Exception:
        return None


def _read_photo_taken_time(json_path: Path) -> Optional[int]:
    return _read_takeout_ts(json_path, "photoTakenTime")


def _read_creation_time(json_path: Path) -> Optional[int]:
    return _read_takeout_ts(json_path, "creationTime")


# -----------------------------
# EXIF helpers
# -----------------------------
def _read_exif_datetimes(media_path: Path) -> List[Tuple[str, datetime]]:
    """
    Lee todos los datetimes EXIF disponibles.
    Devuelve lista de (fuente, datetime_naive) en orden: Original, Digitized, DateTime.
    """
    if Image is None:
        return []

    try:
        with Image.open(media_path) as img:
            exif = img.getexif()
            if not exif:
                return []

            def _tag_name(tag_id: int) -> str:
                if ExifTags is None:
                    return str(tag_id)
                return ExifTags.TAGS.get(tag_id, str(tag_id))

            exif_map = {}
            for tag_id, value in exif.items():
                exif_map[_tag_name(tag_id)] = value

            results = []
            for exif_key, source in (
                ("DateTimeOriginal", SOURCE_EXIF_DATETIME_ORIGINAL),
                ("DateTimeDigitized", SOURCE_EXIF_DATETIME_DIGITIZED),
                ("DateTime", SOURCE_EXIF_DATETIME),
            ):
                val = exif_map.get(exif_key)
                if not val:
                    continue
                try:
                    if isinstance(val, bytes):
                        val_str = val.decode("utf-8", errors="replace")
                    else:
                        val_str = str(val)
                    naive = datetime.strptime(val_str, "%Y:%m:%d %H:%M:%S")
                    results.append((source, naive))
                except Exception:
                    continue
            return results
    except Exception:
        return []


def _read_quicktime_datetimes(media_path: Path) -> List[Tuple[str, datetime]]:
    """
    Lee metadatos QuickTime (CreateDate/MediaCreateDate/TrackCreateDate) de videos
    MP4/MOV/M4V usando PIL (MPO/mp4 support si está disponible). Si no se puede,
    devuelve lista vacía (no es fatal).
    """
    if Image is None:
        return []
    ext = Path(media_path).suffix.lower()
    if ext not in (".mp4", ".mov", ".m4v"):
        return []

    try:
        with Image.open(media_path) as img:
            md = getattr(img, "info", {}) or {}
    except Exception:
        return []

    results = []
    keys = (
        ("MediaCreateDate", SOURCE_QUICKTIME_MEDIA_CREATE),
        ("TrackCreateDate", SOURCE_QUICKTIME_TRACK_CREATE),
        ("CreateDate", SOURCE_QUICKTIME_CREATE),
    )
    for raw_key, source in keys:
        val = md.get(raw_key)
        if not val:
            continue
        try:
            naive = datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S")
            results.append((source, naive))
        except Exception:
            continue
    return results


# -----------------------------
# Filename date parser
# -----------------------------
_FILENAME_PATTERNS = [
    re.compile(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})[_-]?(\d{2})[_-]?(\d{2})"),  # YYYYMMDD_HHMMSS
    re.compile(r"IMG[-_](\d{8})[-_]WA\d{4}"),  # IMG-YYYYMMDD-WA####
    re.compile(r"VID[-_](\d{8})[-_]WA\d{4}"),  # VID-YYYYMMDD-WA####
    re.compile(r"Screenshot[_ ](\d{8})[_ -](\d{2})[.-](\d{2})[.-](\d{2})"),  # Screenshot_YYYYMMDD
    re.compile(r"(20\d{2})-(\d{2})-(\d{2})"),  # YYYY-MM-DD
]


def parse_filename_date(filename: str) -> Optional[datetime]:
    """
    Intenta extraer una fecha válida del nombre de archivo.
    Valida el calendario real. Devuelve datetime UTC (interpretando el instante
    sin zona horaria como hora local) o None.
    """
    stem = Path(filename).stem

    for pat in _FILENAME_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        groups = m.groups()
        try:
            if len(groups) == 6:
                y, mo, d, h, mi, s = (int(g) for g in groups)
                naive = datetime(y, mo, d, h, mi, s)
            elif len(groups) == 4:
                y, mo, d = (int(g) for g in groups[:3])
                h, mi, s = (int(g) for g in groups[3:])
                naive = datetime(y, mo, d, h, mi, s)
            elif len(groups) == 1:
                ymd = groups[0]
                y, mo, d = int(ymd[0:4]), int(ymd[4:6]), int(ymd[6:8])
                naive = datetime(y, mo, d)
            else:  # YYYY-MM-DD
                y, mo, d = (int(g) for g in groups[:3])
                naive = datetime(y, mo, d)
            return naive.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# -----------------------------
# Timezone helpers
# -----------------------------
def to_utc(naive: datetime) -> datetime:
    """Interpreta un datetime naive como local (para representación) y devuelve UTC aware."""
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    return naive.replace(tzinfo=timezone.utc)


def _from_ts_utc(ts: int) -> datetime:
    """Convierte un timestamp Unix a datetime UTC timezone-aware."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def to_local_year(dt_utc: datetime, timezone_mode: str) -> int:
    """Convierte el instante a año local o UTC según timezone_mode."""
    if dt_utc is None:
        return 0
    if str(timezone_mode or "local").lower() == "utc":
        return dt_utc.year
    local = dt_utc.astimezone()
    return local.year


def _fmt_local(dt_utc: datetime, timezone_mode: str) -> Optional[datetime]:
    """Representación local del instante (naive) para reportes."""
    if dt_utc is None:
        return None
    if str(timezone_mode or "local").lower() == "utc":
        return dt_utc.replace(tzinfo=None)
    return dt_utc.astimezone().replace(tzinfo=None)


# -----------------------------
# Core resolver
# -----------------------------
def _is_valid_year_range(dt_utc: datetime, min_valid_year: int, future_tolerance_days: int) -> bool:
    if dt_utc is None:
        return False
    if dt_utc.year < min_valid_year:
        return False
    now = datetime.now(timezone.utc)
    if dt_utc > now:
        from datetime import timedelta
        if dt_utc - now > timedelta(days=future_tolerance_days):
            return False
    return True


def _folder_year_hint(media_path: Path) -> Optional[int]:
    """Detecta un año de 4 dígitos en las carpetas padre (hint, baja confianza)."""
    parts = list(media_path.parts)
    for part in parts:
        m = re.fullmatch(r"(19|20)\d{2}", part)
        if m:
            return int(part)
    return None


def extract_candidates(
    media_path: Path,
    timezone_mode: str = "local",
    min_valid_year: int = 1970,
    future_date_tolerance_days: int = 1,
    allow_filename_date: bool = True,
    allow_mtime_as_capture_date: bool = False,
) -> List[DateCandidate]:
    """Extrae todos los candidatos de fecha para un archivo (sin elegir)."""
    candidates: List[DateCandidate] = []
    mp = Path(media_path)
    conf = dict(DEFAULT_CONFIDENCE)

    def _mk(source: str, dt_utc: datetime, raw: Optional[str] = None,
            source_path: Optional[str] = None, sidecar_path: Optional[str] = None,
            comparable: bool = True, notes: str = "") -> DateCandidate:
        valid = _is_valid_year_range(dt_utc, min_valid_year, future_date_tolerance_days)
        reason = ""
        if not valid:
            if dt_utc is None:
                reason = "no datetime"
            elif dt_utc.year < min_valid_year:
                reason = f"year < min_valid_year ({min_valid_year})"
            else:
                reason = "future date beyond tolerance"
        return DateCandidate(
            source=source,
            datetime_utc=dt_utc,
            datetime_local=_fmt_local(dt_utc, timezone_mode) if dt_utc else None,
            raw_value=raw,
            confidence=conf.get(source, 0),
            source_path=source_path,
            sidecar_path=sidecar_path,
            valid=valid,
            rejection_reason=reason,
            comparable_as_capture_date=comparable,
            notes=notes,
        )

    # Sidecars de Takeout
    all_sidecars = find_takeout_sidecar_json_all(mp)
    if all_sidecars:
        if len(all_sidecars) > 1:
            # Ambigüedad: registramos un candidato inválido para no perder la traza
            candidates.append(DateCandidate(
                source=SOURCE_PHOTO_TAKEN_TIME,
                datetime_utc=None,
                confidence=conf[SOURCE_PHOTO_TAKEN_TIME],
                sidecar_path=str(all_sidecars[0]),
                valid=False,
                rejection_reason="ambiguous sidecar (multiple candidates)",
                notes="; ".join(str(s) for s in all_sidecars),
            ))
        else:
            sc = all_sidecars[0]
            ts = _read_photo_taken_time(sc)
            if ts is not None and ts > 0:
                candidates.append(_mk(
                    SOURCE_PHOTO_TAKEN_TIME, _from_ts_utc(ts), raw=str(ts),
                    source_path=str(sc), sidecar_path=str(sc),
                ))
            ts2 = _read_creation_time(sc)
            if ts2 is not None and ts2 > 0:
                candidates.append(_mk(
                    SOURCE_CREATION_TIME, _from_ts_utc(ts2), raw=str(ts2),
                    source_path=str(sc), sidecar_path=str(sc),
                    notes="upload/creation time, not capture",
                ))

    # EXIF (imágenes)
    for source, naive in _read_exif_datetimes(mp):
        candidates.append(_mk(
            source, to_utc(naive), raw=naive.strftime("%Y:%m:%d %H:%M:%S"),
            source_path=str(mp),
        ))

    # QuickTime (videos)
    for source, naive in _read_quicktime_datetimes(mp):
        candidates.append(_mk(
            source, to_utc(naive), raw=naive.strftime("%Y:%m:%d %H:%M:%S"),
            source_path=str(mp),
        ))

    # Filename
    if allow_filename_date:
        fdt = parse_filename_date(mp.name)
        if fdt:
            candidates.append(_mk(
                SOURCE_FILENAME, fdt, raw=mp.name, source_path=str(mp),
                comparable=True, notes="parsed from filename",
            ))

    # Folder year hint
    hint = _folder_year_hint(mp)
    if hint:
        try:
            naive = datetime(hint, 6, 1)
            candidates.append(_mk(
                SOURCE_FOLDER_YEAR_HINT, to_utc(naive), raw=str(hint),
                source_path=str(mp), comparable=False,
                notes="year hint from parent folder (not a full capture date)",
            ))
        except ValueError:
            pass

    # mtime
    try:
        ts_mtime = int(mp.stat().st_mtime)
        candidates.append(_mk(
            SOURCE_MTIME, _from_ts_utc(ts_mtime), raw=str(ts_mtime),
            source_path=str(mp),
            comparable=allow_mtime_as_capture_date,
            notes="filesystem mtime (may reflect export time)",
        ))
    except Exception:
        pass

    return candidates


def resolve_capture_datetime(
    media_path: Path,
    date_priority: Optional[List[str]] = None,
    timezone_mode: str = "local",
    min_valid_year: int = 1970,
    future_date_tolerance_days: int = 1,
    date_conflict_tolerance_seconds: int = 300,
    allow_filename_date: bool = True,
    allow_mtime_as_capture_date: bool = False,
    low_confidence_date_policy: str = "review",
) -> DateResolution:
    """
    Resuelve la mejor fecha de captura para un archivo individual.

    Devuelve un DateResolution con:
      - selected_datetime (UTC aware)
      - selected_source, selected_confidence, procedencia
      - conflict / requires_review
      - output_year (año local o UTC según timezone_mode)
      - metadata_write_allowed
    """
    candidates = extract_candidates(
        media_path,
        timezone_mode=timezone_mode,
        min_valid_year=min_valid_year,
        future_date_tolerance_days=future_date_tolerance_days,
        allow_filename_date=allow_filename_date,
        allow_mtime_as_capture_date=allow_mtime_as_capture_date,
    )

    priority = list(date_priority) if date_priority else [
        SOURCE_PHOTO_TAKEN_TIME,
        SOURCE_EXIF_DATETIME_ORIGINAL,
        SOURCE_CREATION_TIME,
        SOURCE_MTIME,
    ]
    priority = [str(s).lower().strip() for s in priority]

    # Normalización de fuentes antiguas (compatibilidad)
    legacy_map = {
        "takeout_json": [SOURCE_PHOTO_TAKEN_TIME, SOURCE_CREATION_TIME],
        "exif": [SOURCE_EXIF_DATETIME_ORIGINAL, SOURCE_EXIF_DATETIME_DIGITIZED, SOURCE_EXIF_DATETIME],
    }

    ordered = []
    for src in priority:
        if src in legacy_map:
            for s in legacy_map[src]:
                if s not in ordered:
                    ordered.append(s)
        elif src not in ordered:
            ordered.append(src)

    # Candidatos ordenados por prioridad (respetando date_source_priority).
    # mtime solo se considera si está en prioridad o allow_mtime_as_capture_date.
    eligible = set(ordered)
    if allow_mtime_as_capture_date:
        eligible.add(SOURCE_MTIME)
    valid_candidates = [
        c for c in candidates
        if c.valid and c.source in eligible and c.datetime_utc is not None
    ]
    valid_candidates.sort(key=lambda c: (ordered.index(c.source) if c.source in ordered else 999, -c.confidence))

    # Sidecar ambiguo detectado en extracción
    ambiguous = any(
        c.source == SOURCE_PHOTO_TAKEN_TIME and not c.valid
        and "ambiguous" in c.rejection_reason
        for c in candidates
    )

    resolution = DateResolution(candidates=candidates)

    if not valid_candidates:
        if ambiguous:
            resolution.conflict = CONFLICT_SIDECAR_AMBIGUOUS
            resolution.conflict_reason = "multiple sidecar candidates found"
        else:
            resolution.conflict = CONFLICT_NO_DATE_FOUND
            resolution.conflict_reason = "no valid date evidence"
        resolution.requires_review = True
        resolution.output_year = "_UNKNOWN"
        return resolution

    best = valid_candidates[0]

    # Detectar conflicto de alta confianza (diferencia > tolerancia entre dos fuertes)
    strong = [c for c in valid_candidates if c.confidence >= 90 and c.comparable_as_capture_date]
    conflict = CONFLICT_NONE
    conflict_reason = ""
    if len(strong) >= 2:
        times = sorted(c.datetime_utc for c in strong)
        diff = abs((times[-1] - times[0]).total_seconds())
        if diff > date_conflict_tolerance_seconds:
            conflict = CONFLICT_HIGH_CONFIDENCE
            conflict_reason = (
                f"conflict between {strong[0].source} and {strong[-1].source} "
                f"(diff {int(diff)}s)"
            )

    # Política de baja confianza
    review_low = False
    if best.confidence < 50 and low_confidence_date_policy == "review":
        review_low = True

    only_mtime = best.source == SOURCE_MTIME and not allow_mtime_as_capture_date
    if ambiguous:
        conflict = CONFLICT_SIDECAR_AMBIGUOUS
        conflict_reason = "multiple sidecar candidates found"
    elif only_mtime:
        conflict = CONFLICT_ONLY_MTIME_AVAILABLE
        conflict_reason = "only mtime available and allow_mtime_as_capture_date=false"
    elif best.source == SOURCE_FILENAME and conflict == CONFLICT_NONE:
        conflict = CONFLICT_FILENAME_DATE_ONLY
        conflict_reason = "date derived only from filename"

    requires_review = (
        ambiguous
        or conflict != CONFLICT_NONE
        or review_low
        or not best.comparable_as_capture_date
    )

    resolution.selected_datetime = best.datetime_utc
    resolution.selected_source = best.source
    resolution.selected_confidence = best.confidence
    resolution.selected_source_path = best.source_path
    resolution.selected_sidecar_path = best.sidecar_path
    resolution.conflict = conflict
    resolution.conflict_reason = conflict_reason
    resolution.requires_review = requires_review
    resolution.output_year = str(to_local_year(best.datetime_utc, timezone_mode))
    resolution.metadata_write_allowed = (
        best.comparable_as_capture_date
        and best.confidence >= 50
        and conflict == CONFLICT_NONE
        and not requires_review
    )
    return resolution


# -----------------------------
# Resolución por tipo de grupo
# -----------------------------
def resolve_exact_group_datetime(
    members: Iterable[Path],
    date_priority: Optional[List[str]] = None,
    timezone_mode: str = "local",
    min_valid_year: int = 1970,
    future_date_tolerance_days: int = 1,
    date_conflict_tolerance_seconds: int = 300,
    allow_filename_date: bool = True,
    allow_mtime_as_capture_date: bool = False,
    low_confidence_date_policy: str = "review",
) -> DateResolution:
    """
    Resuelve la fecha canónica de un grupo EXACTO (mismo SHA-256).

    Evalúa la evidencia de TODOS los miembros (archivos + sidecars) y elige la
    mejor. Como los bytes son idénticos, la fecha de captura es la misma, y la
    evidencia puede venir de cualquier copia (se documenta la procedencia).

    Reglas:
      - No se limita a la fecha del winner elegido por ruta.
      - No se usa creationTime/mtime como conflicto si existe photoTakenTime/EXIF.
      - Si hay conflicto entre dos fuentes de alta confianza (>=90), se marca
        requires_review con HIGH_CONFIDENCE_DATE_CONFLICT.
      - La fecha canónica SIEMPRE se toma del mejor candidato del mejor miembro.
    """
    priority = list(date_priority) if date_priority else [
        SOURCE_PHOTO_TAKEN_TIME,
        SOURCE_EXIF_DATETIME_ORIGINAL,
        SOURCE_CREATION_TIME,
        SOURCE_MTIME,
    ]

    # Resolver cada miembro individualmente para recolectar candidatos
    all_candidates: List[DateCandidate] = []
    member_resolutions: List[Tuple[Path, DateResolution]] = []
    for m in members:
        res = resolve_capture_datetime(
            m,
            date_priority=priority,
            timezone_mode=timezone_mode,
            min_valid_year=min_valid_year,
            future_date_tolerance_days=future_date_tolerance_days,
            date_conflict_tolerance_seconds=date_conflict_tolerance_seconds,
            allow_filename_date=allow_filename_date,
            allow_mtime_as_capture_date=allow_mtime_as_capture_date,
            low_confidence_date_policy=low_confidence_date_policy,
        )
        member_resolutions.append((Path(m), res))
        all_candidates.extend(res.candidates)

    # Orden de prioridad normalizado
    legacy_map = {
        "takeout_json": [SOURCE_PHOTO_TAKEN_TIME, SOURCE_CREATION_TIME],
        "exif": [SOURCE_EXIF_DATETIME_ORIGINAL, SOURCE_EXIF_DATETIME_DIGITIZED, SOURCE_EXIF_DATETIME],
    }
    ordered = []
    for src in priority:
        if str(src).lower().strip() in legacy_map:
            for s in legacy_map[str(src).lower().strip()]:
                if s not in ordered:
                    ordered.append(s)
        elif str(src).lower().strip() not in ordered:
            ordered.append(str(src).lower().strip())

    eligible = set(ordered)
    if allow_mtime_as_capture_date:
        eligible.add(SOURCE_MTIME)

    valid = [
        c for c in all_candidates
        if c.valid and c.source in eligible and c.datetime_utc is not None
    ]
    valid.sort(key=lambda c: (ordered.index(c.source) if c.source in ordered else 999, -c.confidence))

    resolution = DateResolution(candidates=all_candidates)

    if not valid:
        resolution.conflict = CONFLICT_NO_DATE_FOUND
        resolution.conflict_reason = "no valid date evidence across any exact member"
        resolution.requires_review = True
        resolution.output_year = "_UNKNOWN"
        return resolution

    best = valid[0]

    # Procedencia: encontrar miembro que aportó el mejor candidato
    source_member = None
    for member, res in member_resolutions:
        for c in res.candidates:
            if c is best:
                source_member = member
                break
        if source_member:
            break

    # ¿La mejor evidencia proviene de un miembro distinto al "mejor" por nombre?
    # (la decisión de winner la toma el planner/dedupe; aquí solo documentamos)
    borrowed = False
    if source_member is not None and best.source_path:
        member_hint = Path(best.source_path)
        if source_member != member_hint:
            borrowed = True

    # Conflicto de alta confianza: dos fuentes >=90 con diferencia > tolerancia
    strong = [c for c in valid if c.confidence >= 90 and c.comparable_as_capture_date]
    conflict = CONFLICT_NONE
    conflict_reason = ""
    if len(strong) >= 2:
        times = sorted(c.datetime_utc for c in strong)
        diff = abs((times[-1] - times[0]).total_seconds())
        if diff > date_conflict_tolerance_seconds:
            conflict = CONFLICT_HIGH_CONFIDENCE
            conflict_reason = (
                f"conflict between {strong[0].source} and {strong[-1].source} "
                f"(diff {int(diff)}s)"
            )

    # Ambiguos en cualquier miembro
    ambiguous = any(
        c.source == SOURCE_PHOTO_TAKEN_TIME and not c.valid
        and "ambiguous" in c.rejection_reason
        for c in all_candidates
    )

    # Política de baja confianza (mismas reglas que el resolver individual)
    review_low = False
    if best.confidence < 50 and low_confidence_date_policy == "review":
        review_low = True

    only_mtime = best.source == SOURCE_MTIME and not allow_mtime_as_capture_date
    if ambiguous and conflict == CONFLICT_NONE:
        conflict = CONFLICT_SIDECAR_AMBIGUOUS
        conflict_reason = "multiple sidecar candidates found in exact group"
    elif only_mtime and conflict == CONFLICT_NONE:
        conflict = CONFLICT_ONLY_MTIME_AVAILABLE
        conflict_reason = "only mtime available and allow_mtime_as_capture_date=false"
    elif best.source == SOURCE_FILENAME and conflict == CONFLICT_NONE:
        conflict = CONFLICT_FILENAME_DATE_ONLY
        conflict_reason = "date derived only from filename"

    requires_review = (
        conflict != CONFLICT_NONE
        or ambiguous
        or review_low
        or not best.comparable_as_capture_date
    )

    resolution.selected_datetime = best.datetime_utc
    resolution.selected_source = best.source
    resolution.selected_confidence = best.confidence
    resolution.selected_source_path = best.source_path
    resolution.selected_sidecar_path = best.sidecar_path
    resolution.conflict = conflict
    resolution.conflict_reason = conflict_reason
    resolution.requires_review = requires_review
    resolution.output_year = str(to_local_year(best.datetime_utc, timezone_mode))
    resolution.metadata_write_allowed = (
        best.comparable_as_capture_date
        and best.confidence >= 50
        and not requires_review
    )
    resolution.canonical_source_member = str(source_member) if source_member else None
    if borrowed and resolution.selected_source in (SOURCE_PHOTO_TAKEN_TIME, SOURCE_EXIF_DATETIME_ORIGINAL):
        resolution.conflict_reason = (
            f"{resolution.conflict_reason}; {CONFLICT_GROUP_DATE_BORROWED}: "
            f"canonical evidence from member {resolution.canonical_source_member}"
        ).strip("; ").lstrip("; ")

    return resolution


def resolve_perceptual_member_datetime(
    member: Path,
    date_priority: Optional[List[str]] = None,
    timezone_mode: str = "local",
    **kwargs,
) -> DateResolution:
    """
    Resuelve la fecha INDIVIDUAL de un miembro perceptual.

    Cada miembro de un grupo perceptual conserva su propia fecha (los fotos
    perceptualmente similares pueden ser ráfagas/fotos distintas). NUNCA se
    propaga la fecha de otro miembro.
    """
    return resolve_capture_datetime(member, date_priority=date_priority, timezone_mode=timezone_mode, **kwargs)


# -----------------------------
# Public wrappers (API compatible)
# -----------------------------
def get_capture_datetime(
    media_path: Path,
    date_priority: List[str],
    timezone_mode: str = "local",
) -> Optional[datetime]:
    """
    Public: returns capture datetime or None (UTC aware).
    """
    res = resolve_capture_datetime(media_path, date_priority=date_priority, timezone_mode=timezone_mode)
    return res.selected_datetime


def get_date_source_used(
    media_path: Path,
    date_priority: List[str],
    timezone_mode: str = "local",
) -> str:
    """
    Public: returns which source produced the capture datetime.
    """
    res = resolve_capture_datetime(media_path, date_priority=date_priority, timezone_mode=timezone_mode)
    if res.selected_source == SOURCE_PHOTO_TAKEN_TIME:
        return "takeout_json"
    if res.selected_source in (SOURCE_EXIF_DATETIME_ORIGINAL, SOURCE_EXIF_DATETIME_DIGITIZED, SOURCE_EXIF_DATETIME):
        return "exif"
    return res.selected_source


def get_capture_year(
    media_path: Path,
    date_priority: List[str],
    unknown_year_dir: str = "_UNKNOWN",
    timezone_mode: str = "local",
) -> str:
    """
    Public: returns year string ("2019") or unknown_year_dir.
    """
    res = resolve_capture_datetime(media_path, date_priority=date_priority, timezone_mode=timezone_mode)
    if not res.selected_datetime:
        return unknown_year_dir
    return res.output_year if res.output_year else unknown_year_dir


def get_capture_year_for_group(group_or_winner: Any, date_priority, unknown_year_dir, timezone_mode) -> str:
    """
    Returns year for a duplicate group using the group's winner date.
    Accepts either:
      - DuplicateGroup-like object with attribute .winner
      - a Path/str directly pointing to the winner file
    """
    if hasattr(group_or_winner, "winner"):
        winner_path = Path(group_or_winner.winner)
    else:
        winner_path = Path(group_or_winner)

    return get_capture_year(winner_path, date_priority, unknown_year_dir, timezone_mode)


# -----------------------------
# Account inference
# -----------------------------
def infer_account(file_path: str, inputs: Tuple[str, ...]) -> str:
    """
    Determine which configured input directory a file belongs to.
    Returns the input root folder name (e.g., the email folder), or "" if not matched.
    """
    try:
        fp = Path(file_path).resolve()
    except Exception:
        fp = Path(file_path)

    for inp in inputs:
        try:
            root = Path(inp).resolve()
        except Exception:
            root = Path(inp)

        try:
            fp.relative_to(root)
            return root.name
        except Exception:
            continue

    return ""
