"""Tests para el modelo de evidencia de fechas (Fase 1)."""

import json
import os
from pathlib import Path
from datetime import datetime, timezone

from PIL import Image

from photos_dedupe.date_models import (
    CONFLICT_FILENAME_DATE_ONLY,
    CONFLICT_GROUP_DATE_BORROWED,
    CONFLICT_NO_DATE_FOUND,
    CONFLICT_ONLY_MTIME_AVAILABLE,
    CONFLICT_SIDECAR_AMBIGUOUS,
    SOURCE_CREATION_TIME,
    SOURCE_EXIF_DATETIME_ORIGINAL,
    SOURCE_FILENAME,
    SOURCE_MTIME,
    SOURCE_PHOTO_TAKEN_TIME,
)
from photos_dedupe.date_utils import (
    extract_candidates,
    find_takeout_sidecar_json,
    find_takeout_sidecar_json_all,
    parse_filename_date,
    resolve_capture_datetime,
    resolve_exact_group_datetime,
    resolve_perceptual_member_datetime,
    to_utc,
)

TS_2022 = 1664654748  # 2022-10-01
TS_2026 = 1770000000  # ~2026


def _sidecar(media: Path, name: str, ts: int, key: str = "photoTakenTime") -> Path:
    p = media.with_name(name)
    p.write_text(json.dumps({key: {"timestamp": str(ts)}}), encoding="utf-8")
    return p


def _media(tmp_path, name="p.jpg", ts_mtime=None) -> Path:
    p = tmp_path / name
    p.write_bytes(b"x")
    if ts_mtime:
        os.utime(p, (ts_mtime, ts_mtime))
    return p


# ---------- sidecars: 4 patrones ----------

def test_sidecar_pattern_ext_json(tmp_path):
    m = _media(tmp_path)
    sc = _sidecar(m, "p.jpg.json", TS_2022)
    assert find_takeout_sidecar_json(m) == sc


def test_sidecar_pattern_ext_supplemental(tmp_path):
    m = _media(tmp_path)
    sc = _sidecar(m, "p.jpg.supplemental-metadata.json", TS_2022)
    assert find_takeout_sidecar_json(m) == sc


def test_sidecar_pattern_stem_json(tmp_path):
    m = _media(tmp_path)
    sc = _sidecar(m, "p.json", TS_2022)
    assert find_takeout_sidecar_json(m) == sc


def test_sidecar_pattern_stem_supplemental(tmp_path):
    m = _media(tmp_path)
    sc = _sidecar(m, "p.supplemental-metadata.json", TS_2022)
    assert find_takeout_sidecar_json(m) == sc


def test_sidecar_ambiguous_returns_none(tmp_path):
    m = _media(tmp_path)
    _sidecar(m, "p.jpg.json", TS_2022)
    _sidecar(m, "p.json", TS_2022)
    assert find_takeout_sidecar_json(m) is None
    assert len(find_takeout_sidecar_json_all(m)) == 2


def test_sidecar_ambiguous_marks_review(tmp_path):
    m = _media(tmp_path)
    _sidecar(m, "p.jpg.json", TS_2022)
    _sidecar(m, "p.json", TS_2022)
    res = resolve_capture_datetime(m, date_priority=[SOURCE_PHOTO_TAKEN_TIME, "mtime"])
    assert res.requires_review is True
    assert res.conflict == CONFLICT_SIDECAR_AMBIGUOUS


# ---------- takeout: photoTakenTime vs creationTime ----------

def test_photo_taken_time_beats_creation_time(tmp_path):
    m = _media(tmp_path, ts_mtime=TS_2026)
    # Un solo sidecar con ambas claves (patrón real de Takeout)
    sc = m.with_name("p.jpg.json")
    sc.write_text(json.dumps({
        "photoTakenTime": {"timestamp": str(TS_2022)},
        "creationTime": {"timestamp": str(TS_2026)},
    }), encoding="utf-8")
    candidates = extract_candidates(m)
    taken = [c for c in candidates if c.source == SOURCE_PHOTO_TAKEN_TIME]
    created = [c for c in candidates if c.source == SOURCE_CREATION_TIME]
    assert len(taken) == 1 and len(created) == 1
    assert taken[0].datetime_utc.year == 2022
    assert created[0].datetime_utc.year == 2026
    assert taken[0].sidecar_path == str(sc)
    # photoTakenTime (conf 100) debe ganar a creationTime (conf 70)
    res = resolve_capture_datetime(m, date_priority=[SOURCE_PHOTO_TAKEN_TIME, SOURCE_CREATION_TIME, SOURCE_MTIME])
    assert res.selected_source == SOURCE_PHOTO_TAKEN_TIME
    assert res.output_year == "2022"


# ---------- EXIF ----------

def _jpg_with_exif(path: Path, tag_id: int, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (8, 8), (5, 5, 5))
    exif = Image.Exif()
    exif[tag_id] = value
    img.save(path, exif=exif, format="JPEG")
    return path


def test_exif_datetime_original_candidate(tmp_path):
    p = _jpg_with_exif(tmp_path / "e.jpg", 36867, b"2019:03:15 12:30:00")
    cands = extract_candidates(p)
    orig = [c for c in cands if c.source == SOURCE_EXIF_DATETIME_ORIGINAL]
    assert len(orig) == 1
    assert orig[0].datetime_utc.year == 2019


def test_exif_invalid_date_rejected(tmp_path):
    p = _jpg_with_exif(tmp_path / "bad.jpg", 36867, b"not-a-date")
    cands = extract_candidates(p)
    assert all(c.source != SOURCE_EXIF_DATETIME_ORIGINAL for c in cands if c.valid)


# ---------- filename ----------

def test_filename_parse_wa_pattern():
    dt = parse_filename_date("IMG-20221001-WA0045.jpg")
    assert dt is not None
    assert dt.year == 2022 and dt.month == 10 and dt.day == 1


def test_filename_parse_underscore():
    dt = parse_filename_date("IMG_20221001_123456.jpg")
    assert dt is not None
    assert dt.year == 2022 and dt.hour == 12 and dt.minute == 34


def test_filename_parse_screenshot():
    dt = parse_filename_date("Screenshot_20220203-101520.png")
    assert dt is not None
    assert (dt.year, dt.month, dt.day) == (2022, 2, 3)


def test_filename_parse_rejects_fake_numbers():
    assert parse_filename_date("12345678.jpg") is None
    assert parse_filename_date("foto_99999999.jpg") is None
    # 20231340 = mes 13 inválido
    assert parse_filename_date("IMG-20231340-WA0001.jpg") is None


def test_filename_date_used_as_low_confidence(tmp_path):
    m = _media(tmp_path, name="IMG-20221001-WA0001.jpg", ts_mtime=TS_2026)
    res = resolve_capture_datetime(m, date_priority=[SOURCE_FILENAME, SOURCE_MTIME])
    assert res.selected_source == SOURCE_FILENAME
    assert res.output_year == "2022"
    assert res.conflict == CONFLICT_FILENAME_DATE_ONLY
    assert res.requires_review is True
    assert res.metadata_write_allowed is False


def test_filename_disabled(tmp_path):
    m = _media(tmp_path, name="IMG-20221001-WA0001.jpg")
    res = resolve_capture_datetime(
        m, date_priority=[SOURCE_FILENAME, SOURCE_MTIME], allow_filename_date=False,
        allow_mtime_as_capture_date=True,
    )
    assert res.selected_source == SOURCE_MTIME


# ---------- validación temporal ----------

def test_future_date_rejected(tmp_path):
    m = _media(tmp_path)
    _sidecar(m, "p.jpg.json", 4102444800)  # año 2100
    res = resolve_capture_datetime(m, date_priority=[SOURCE_PHOTO_TAKEN_TIME, SOURCE_MTIME],
                                   future_date_tolerance_days=1, allow_mtime_as_capture_date=True)
    assert res.selected_datetime is None or res.selected_datetime.year < 2050
    assert res.conflict != CONFLICT_NO_DATE_FOUND  # mtime válido fue usado


def test_pre_1970_rejected(tmp_path):
    m = _media(tmp_path)
    _sidecar(m, "p.jpg.json", 0)
    res = resolve_capture_datetime(m, date_priority=[SOURCE_PHOTO_TAKEN_TIME])
    assert res.conflict == CONFLICT_NO_DATE_FOUND


# ---------- mtime policy ----------

def test_mtime_only_not_allowed(tmp_path):
    m = _media(tmp_path, ts_mtime=TS_2026)
    res = resolve_capture_datetime(m, date_priority=[SOURCE_PHOTO_TAKEN_TIME, SOURCE_MTIME],
                                   allow_mtime_as_capture_date=False)
    assert res.conflict == CONFLICT_ONLY_MTIME_AVAILABLE
    assert res.requires_review is True


def test_mtime_only_allowed(tmp_path):
    m = _media(tmp_path, ts_mtime=TS_2026)
    res = resolve_capture_datetime(m, date_priority=[SOURCE_PHOTO_TAKEN_TIME, SOURCE_MTIME],
                                   allow_mtime_as_capture_date=True)
    assert res.selected_source == SOURCE_MTIME
    assert res.conflict != CONFLICT_NO_DATE_FOUND


# ---------- datetimes aware ----------

def test_utc_aware_from_sidecar(tmp_path):
    m = _media(tmp_path)
    _sidecar(m, "p.jpg.json", TS_2022)
    cands = extract_candidates(m, timezone_mode="local")
    taken = [c for c in cands if c.source == SOURCE_PHOTO_TAKEN_TIME][0]
    assert taken.datetime_utc.tzinfo is not None
    assert taken.datetime_utc.utcoffset().total_seconds() == 0


def test_to_utc_keeps_instant():
    naive = datetime(2022, 10, 1, 12, 0, 0)
    assert to_utc(naive).tzinfo is not None


def test_to_utc_interprets_naive_as_local(monkeypatch):
    """Regresión: to_utc debe interpretar el naive como hora LOCAL (cámara) y
    convertir a UTC, no estamparlo como UTC a secas. Con la máquina en UTC-6,
    el naive 06:55 local debe quedar en 12:55 UTC (no 06:55 UTC)."""
    from datetime import timedelta
    import photos_dedupe.date_utils as du

    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2021, 1, 1, 12, 0, 0)

        def astimezone(self, tz=None):
            # simula zona local fija UTC-6 en cualquier máquina
            return self.replace(tzinfo=timezone(timedelta(hours=-6)))

    monkeypatch.setattr(du, "datetime", _FixedNow)
    naive = datetime(2020, 12, 26, 6, 55, 36)  # hora de pared de la cámara
    utc = du.to_utc(naive)
    assert utc.utcoffset().total_seconds() == 0
    assert utc == datetime(2020, 12, 26, 12, 55, 36, tzinfo=timezone.utc)


def test_local_wall_exif_matches_utc_phototaken(tmp_path):
    """Regresión conflictos-6h: un EXIF que guarda la hora local del mismo instante
    que un sidecar photoTakenTime (UTC) NO debe generar conflicto de alta confianza."""
    from datetime import timedelta
    import time as _time
    from photos_dedupe.date_models import CONFLICT_NONE

    instant = datetime(2020, 12, 26, 12, 55, 36, tzinfo=timezone.utc)  # 1664654748 ≈ 2022; usamos 2020
    ts = int(instant.timestamp())
    f = tmp_path / "foto.jpg"
    Image.new("RGB", (8, 8), (1, 2, 3)).save(f, format="JPEG")
    _sidecar(f, "foto.jpg.json", ts)  # photoTakenTime = mismo instante en UTC
    # EXIF = el mismo instante expresado como hora de pared local (como guarda una cámara)
    local_wall = instant.astimezone().replace(tzinfo=None).strftime("%Y:%m:%d %H:%M:%S")
    with Image.open(f) as img:
        ex = img.getexif()
        ex[0x9003] = local_wall
        img.save(f, format="JPEG", exif=ex)

    r = resolve_capture_datetime(f)
    assert r.conflict == CONFLICT_NONE, r.conflict_reason
    assert r.selected_source == SOURCE_PHOTO_TAKEN_TIME
    assert r.output_year == "2020"
    assert r.requires_review is False


# ---------- grupo exacto: fecha canónica (regresión WA0045) ----------

def _same_bytes(p: Path, data: bytes) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_exact_group_canonical_date_from_other_member(tmp_path):
    """
    Regresión equivalente al caso real IMG-20221001-WA0045.jpg:
    - Copia A (ruta alfabéticamente anterior): sin sidecar, mtime 2026.
    - Copia B: con sidecar photoTakenTime 2022.
    El grupo exacto debe usar la fecha canónica 2022, no el mtime 2026.
    """
    data = b"IDENTICAL-BYTE-CONTENT-WA0045"
    mtime_2026 = 1770000000

    copy_a = _same_bytes(tmp_path / "Fotos del 2022" / "IMG-20221001-WA0045.jpg", data)
    copy_b = _same_bytes(tmp_path / "Nuestros recuerdos(5)" / "IMG-20221001-WA0045.jpg", data)
    os.utime(copy_a, (mtime_2026, mtime_2026))
    os.utime(copy_b, (mtime_2026, mtime_2026))
    _sidecar(copy_b, "IMG-20221001-WA0045.jpg.supplemental-metadata.json", TS_2022)

    res = resolve_exact_group_datetime(
        [copy_a, copy_b],
        date_priority=[SOURCE_PHOTO_TAKEN_TIME, SOURCE_MTIME],
        timezone_mode="local",
        allow_mtime_as_capture_date=False,
    )
    assert res.output_year == "2022"
    assert res.selected_source == SOURCE_PHOTO_TAKEN_TIME
    assert res.selected_datetime.year == 2022
    # la evidencia provino de la copia B (con sidecar), no de la A
    assert res.canonical_source_member == str(copy_b)
    assert "WA0045" in res.selected_sidecar_path or "WA0045" in (res.selected_source_path or "")


def test_exact_group_borrowed_evidence_documented(tmp_path):
    """La procedencia debe documentar que la fecha vino de otra copia exacta."""
    data = b"BYTES-EXACTOS"
    copy_a = _same_bytes(tmp_path / "Fotos del 2022" / "IMG-20221001-WA0045.jpg", data)
    copy_b = _same_bytes(tmp_path / "Nuestros recuerdos(5)" / "IMG-20221001-WA0045.jpg", data)
    os.utime(copy_a, (1770000000, 1770000000))
    _sidecar(copy_b, "IMG-20221001-WA0045.jpg.supplemental-metadata.json", TS_2022)

    res = resolve_exact_group_datetime(
        [copy_a, copy_b],
        date_priority=[SOURCE_PHOTO_TAKEN_TIME, SOURCE_MTIME],
        timezone_mode="local",
        allow_mtime_as_capture_date=False,
    )
    assert res.canonical_source_member == str(copy_b)
    assert "GROUP_DATE_BORROWED" in res.conflict_reason


def test_exact_group_mtime_not_allowed_when_json_exists(tmp_path):
    """El mtime de exportación no debe ganar sobre un JSON válido."""
    data = b"EXACT-BYTES-2"
    a = _same_bytes(tmp_path / "carpeta1" / "f.jpg", data)
    b = _same_bytes(tmp_path / "carpeta2" / "f.jpg", data)
    os.utime(a, (1770000000, 1770000000))  # 2026
    _sidecar(b, "f.jpg.supplemental-metadata.json", TS_2022)
    res = resolve_exact_group_datetime(
        [a, b],
        date_priority=[SOURCE_PHOTO_TAKEN_TIME, SOURCE_MTIME],
        allow_mtime_as_capture_date=False,
    )
    assert res.selected_source == SOURCE_PHOTO_TAKEN_TIME
    assert res.output_year == "2022"


def test_exact_group_no_date_at_all(tmp_path):
    data = b"EXACT-NO-DATE"
    a = _same_bytes(tmp_path / "a" / "f.jpg", data)
    b = _same_bytes(tmp_path / "b" / "f.jpg", data)
    os.utime(a, (86400, 86400))  # 1970
    os.utime(b, (86400, 86400))
    res = resolve_exact_group_datetime(
        [a, b],
        date_priority=[SOURCE_PHOTO_TAKEN_TIME],  # mtime不在prioridad → no eligible
        allow_mtime_as_capture_date=False,
    )
    assert res.conflict == CONFLICT_NO_DATE_FOUND
    assert res.output_year == "_UNKNOWN"


# ---------- grupo perceptual: fechas individuales ----------

def test_perceptual_members_keep_individual_dates(tmp_path):
    """Cada miembro perceptual conserva su propia fecha (sin propagación)."""
    data_a = b"A-DIFFERENT-CONTENT"
    data_b = b"B-DIFFERENT-CONTENT"
    mtime_2022 = 1664654748
    mtime_2026 = 1770000000
    a = _same_bytes(tmp_path / "a.jpg", data_a)
    b = _same_bytes(tmp_path / "b.jpg", data_b)
    os.utime(a, (mtime_2022, mtime_2022))
    os.utime(b, (mtime_2026, mtime_2026))
    # mismo directorio, distinto contenido → distinto SHA → candidatos perceptuales
    ra = resolve_perceptual_member_datetime(
        a, date_priority=[SOURCE_MTIME], allow_mtime_as_capture_date=True, timezone_mode="local")
    rb = resolve_perceptual_member_datetime(
        b, date_priority=[SOURCE_MTIME], allow_mtime_as_capture_date=True, timezone_mode="local")
    assert ra.output_year == "2022"
    assert rb.output_year == "2026"
    # No se propaga: cada resolución es independiente del otro miembro
    assert ra.selected_source == SOURCE_MTIME
    assert rb.selected_source == SOURCE_MTIME
