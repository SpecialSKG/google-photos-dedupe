"""
date_models.py

Modelo de evidencia de fechas: candidatos, resolución y niveles de confianza.

Cada fecha posible para un archivo se representa como un DateCandidate con:
  - fuente (source)
  - datetime timezone-aware (UTC)
  - confianza (confidence)
  - procedencia (source_path / sidecar_path)
  - validez y motivo de rechazo

La resolución (DateResolution) elige el mejor candidato y deja trazabilidad
de conflictos, año de salida y si la fecha es apta para escribir metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


# -----------------------------
# Fuentes de fecha normalizadas
# -----------------------------
SOURCE_PHOTO_TAKEN_TIME = "takeout_photo_taken_time"
SOURCE_CREATION_TIME = "takeout_creation_time"
SOURCE_EXIF_DATETIME_ORIGINAL = "exif_datetime_original"
SOURCE_EXIF_DATETIME_DIGITIZED = "exif_datetime_digitized"
SOURCE_EXIF_DATETIME = "exif_datetime"
SOURCE_QUICKTIME_MEDIA_CREATE = "quicktime_media_create_date"
SOURCE_QUICKTIME_TRACK_CREATE = "quicktime_track_create_date"
SOURCE_QUICKTIME_CREATE = "quicktime_create_date"
SOURCE_FILENAME = "filename"
SOURCE_FOLDER_YEAR_HINT = "folder_year_hint"
SOURCE_MTIME = "mtime"

# Confianzas iniciales (deterministas, configurables por override)
DEFAULT_CONFIDENCE = {
    SOURCE_PHOTO_TAKEN_TIME: 100,
    SOURCE_EXIF_DATETIME_ORIGINAL: 95,
    SOURCE_QUICKTIME_MEDIA_CREATE: 92,
    SOURCE_QUICKTIME_TRACK_CREATE: 90,
    SOURCE_QUICKTIME_CREATE: 88,
    SOURCE_EXIF_DATETIME_DIGITIZED: 85,
    SOURCE_EXIF_DATETIME: 80,
    SOURCE_CREATION_TIME: 70,
    SOURCE_FILENAME: 60,
    SOURCE_FOLDER_YEAR_HINT: 35,
    SOURCE_MTIME: 20,
}

# Códigos de conflicto legibles
CONFLICT_NONE = "NONE"
CONFLICT_NO_DATE_FOUND = "NO_DATE_FOUND"
CONFLICT_ONLY_MTIME_AVAILABLE = "ONLY_MTIME_AVAILABLE"
CONFLICT_SIDECAR_AMBIGUOUS = "SIDECAR_AMBIGUOUS"
CONFLICT_HIGH_CONFIDENCE = "HIGH_CONFIDENCE_DATE_CONFLICT"
CONFLICT_FUTURE_DATE_REJECTED = "FUTURE_DATE_REJECTED"
CONFLICT_INVALID_EXIF = "INVALID_EXIF_DATE"
CONFLICT_FILENAME_DATE_ONLY = "FILENAME_DATE_ONLY"
CONFLICT_GROUP_DATE_BORROWED = "GROUP_DATE_BORROWED_FROM_EXACT_COPY"
CONFLICT_LOW_CONFIDENCE = "LOW_CONFIDENCE_DATE"


@dataclass
class DateCandidate:
    """Una fecha candidata individual para un archivo."""
    source: str
    datetime_utc: Optional[datetime] = None        # timezone-aware UTC
    datetime_local: Optional[datetime] = None       # representación local (si aplica)
    raw_value: Optional[str] = None                 # valor original (ej. "2022:10:01 12:00:00")
    confidence: int = 0
    source_path: Optional[str] = None               # archivo donde se halló la evidencia
    sidecar_path: Optional[str] = None              # sidecar JSON si la evidencia viene de él
    valid: bool = True
    rejection_reason: str = ""
    comparable_as_capture_date: bool = True         # False para folder_year_hint, etc.
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "datetime_utc": self.datetime_utc.isoformat() if self.datetime_utc else None,
            "datetime_local": self.datetime_local.isoformat() if self.datetime_local else None,
            "raw_value": self.raw_value,
            "confidence": self.confidence,
            "source_path": self.source_path,
            "sidecar_path": self.sidecar_path,
            "valid": self.valid,
            "rejection_reason": self.rejection_reason,
            "comparable_as_capture_date": self.comparable_as_capture_date,
            "notes": self.notes,
        }


@dataclass
class DateResolution:
    """Resultado de la resolución de fecha para un archivo o grupo."""
    selected_datetime: Optional[datetime] = None
    selected_source: str = ""
    selected_confidence: int = 0
    selected_source_path: Optional[str] = None
    selected_sidecar_path: Optional[str] = None
    candidates: List[DateCandidate] = field(default_factory=list)
    conflict: str = CONFLICT_NONE
    conflict_reason: str = ""
    requires_review: bool = False
    output_year: str = "_UNKNOWN"
    metadata_write_allowed: bool = False
    # Para grupos exactos: de dónde salió la evidencia canónica
    canonical_source_member: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "selected_datetime": self.selected_datetime.isoformat() if self.selected_datetime else None,
            "selected_source": self.selected_source,
            "selected_confidence": self.selected_confidence,
            "selected_source_path": self.selected_source_path,
            "selected_sidecar_path": self.selected_sidecar_path,
            "candidates": [c.to_dict() for c in self.candidates],
            "conflict": self.conflict,
            "conflict_reason": self.conflict_reason,
            "requires_review": self.requires_review,
            "output_year": self.output_year,
            "metadata_write_allowed": self.metadata_write_allowed,
            "canonical_source_member": self.canonical_source_member,
        }
