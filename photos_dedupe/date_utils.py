"""
Utilities for extracting capture dates (year) from:
- Google Takeout JSON sidecars
- EXIF metadata
- File modification time (mtime)
Also includes account inference helpers.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Any

logger = logging.getLogger(__name__)

try:
    from PIL import Image, ExifTags
except Exception:
    Image = None
    ExifTags = None


# -----------------------------
# Sidecar JSON helpers
# -----------------------------
def find_takeout_sidecar_json(media_path: Path) -> Optional[Path]:
    """
    Locate Google Takeout JSON sidecar for a given media file.
    Common patterns:
      - IMG_0001.JPG.json
      - IMG_0001.JPG.supplemental-metadata.json
      - IMG_0001.json
      - IMG_0001.supplemental-metadata.json
    """
    if not media_path:
        return None

    candidates = []

    # filename with extension + .json
    candidates.append(media_path.with_name(media_path.name + ".json"))
    candidates.append(media_path.with_name(media_path.name + ".supplemental-metadata.json"))

    # stem-based
    candidates.append(media_path.with_name(media_path.stem + ".json"))
    candidates.append(media_path.with_name(media_path.stem + ".supplemental-metadata.json"))

    for c in candidates:
        try:
            if c.exists():
                return c
        except Exception:
            pass

    return None


def _read_takeout_timestamp(json_path: Path) -> Optional[int]:
    """
    Read a unix timestamp from Takeout JSON, preferring photoTakenTime then creationTime.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    # Google Takeout commonly uses:
    # data["photoTakenTime"]["timestamp"] or data["creationTime"]["timestamp"]
    for key in ("photoTakenTime", "creationTime"):
        try:
            node = data.get(key)
            if isinstance(node, dict):
                ts = node.get("timestamp")
                if ts is None:
                    continue
                # ts might be string
                ts_int = int(ts)
                if ts_int > 0:
                    return ts_int
        except Exception:
            continue

    return None


# -----------------------------
# EXIF helpers
# -----------------------------
def _read_exif_datetime(media_path: Path) -> Optional[datetime]:
    """
    Try to read EXIF datetime from an image.
    Looks for DateTimeOriginal, DateTimeDigitized, DateTime.
    Returns naive datetime.
    """
    if Image is None:
        return None

    try:
        with Image.open(media_path) as img:
            exif = img.getexif()
            if not exif:
                return None

            # Map tag ids to names
            # (ExifTags.TAGS exists when PIL is properly installed)
            def _tag_name(tag_id: int) -> str:
                if ExifTags is None:
                    return str(tag_id)
                return ExifTags.TAGS.get(tag_id, str(tag_id))

            exif_map = {}
            for tag_id, value in exif.items():
                exif_map[_tag_name(tag_id)] = value

            for exif_key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
                val = exif_map.get(exif_key)
                if not val:
                    continue

                # EXIF date format: "YYYY:MM:DD HH:MM:SS"
                try:
                    return datetime.strptime(str(val), "%Y:%m:%d %H:%M:%S")
                except Exception:
                    continue

    except Exception:
        return None

    return None


# -----------------------------
# Capture datetime core
# -----------------------------
def _to_dt_from_ts(ts: int, timezone_mode: str) -> datetime:
    """
    Convert unix timestamp to datetime.
    timezone_mode:
      - "local": datetime.fromtimestamp
      - "utc":   datetime.utcfromtimestamp
    Returns naive datetime.
    """
    if timezone_mode and str(timezone_mode).lower() == "utc":
        return datetime.utcfromtimestamp(ts)
    return datetime.fromtimestamp(ts)


@lru_cache(maxsize=200000)
def _get_capture_datetime_cached(
    path_str: str,
    priority: Tuple[str, ...],
    timezone_mode: str,
) -> Tuple[Optional[datetime], str]:
    """
    Cached capture datetime extraction.
    Returns (datetime_or_none, source_used)
    source_used: "takeout_json" | "exif" | "mtime" | ""
    """
    p = Path(path_str)

    for src in priority:
        src = str(src).lower().strip()

        if src == "takeout_json":
            sidecar = find_takeout_sidecar_json(p)
            if sidecar:
                ts = _read_takeout_timestamp(sidecar)
                if ts:
                    return _to_dt_from_ts(ts, timezone_mode), "takeout_json"

        elif src == "exif":
            dt = _read_exif_datetime(p)
            if dt:
                return dt, "exif"

        elif src == "mtime":
            try:
                ts = int(p.stat().st_mtime)
                if ts > 0:
                    return _to_dt_from_ts(ts, timezone_mode), "mtime"
            except Exception:
                pass

    return None, ""


def get_capture_datetime(
    media_path: Path,
    date_priority: List[str],
    timezone_mode: str = "local",
) -> Optional[datetime]:
    """
    Public: returns capture datetime or None.
    """
    prio = tuple(date_priority) if date_priority else ("takeout_json", "exif", "mtime")
    dt, _src = _get_capture_datetime_cached(str(media_path), prio, timezone_mode or "local")
    return dt


def get_date_source_used(
    media_path: Path,
    date_priority: List[str],
    timezone_mode: str = "local",
) -> str:
    """
    Public: returns which source produced the capture datetime.
    """
    prio = tuple(date_priority) if date_priority else ("takeout_json", "exif", "mtime")
    _dt, src = _get_capture_datetime_cached(str(media_path), prio, timezone_mode or "local")
    return src


def get_capture_year(
    media_path: Path,
    date_priority: List[str],
    unknown_year_dir: str = "_UNKNOWN",
    timezone_mode: str = "local",
) -> str:
    """
    Public: returns year string ("2019") or unknown_year_dir.
    """
    dt = get_capture_datetime(media_path, date_priority, timezone_mode)
    if not dt:
        return unknown_year_dir
    try:
        return str(dt.year)
    except Exception:
        return unknown_year_dir


def get_capture_year_for_group(
    group: Any,
    date_priority: List[str],
    unknown_year_dir: str = "_UNKNOWN",
    timezone_mode: str = "local",
) -> str:
    """
    Used by CLI: determine a group's year based on winner path (group.winner).
    """
    return get_capture_year(Path(group.winner), date_priority, unknown_year_dir, timezone_mode)


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
            # Python 3.9+: relative_to throws if not relative
            fp.relative_to(root)
            return root.name
        except Exception:
            continue

    return ""
