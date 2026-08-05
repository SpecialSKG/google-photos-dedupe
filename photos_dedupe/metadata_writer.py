"""
metadata_writer.py (Fase 6)

Escritura/auditoría de metadatos de captura sobre los archivos ya copiados
al out_dir. NUNCA toca los exports.

Modos (config.metadata_mode):
  - disabled : no hace nada.
  - audit    : solo inspecciona y reporta (default; no requiere ExifTool).
  - write    : escribe DateTimeOriginal vía ExifTool (opcional) y verifica.

Reglas de seguridad:
  - Si ExifTool no está disponible y metadata_mode=write, no se escribe nada;
    la operación queda reportada como EXIFTOOL_NOT_AVAILABLE (y el archivo
    sigue en su bucket normal — jamás se pierde el archivo por metadata).
  - Las escrituras solo ocurren sobre archivos DENTRO del run_dir (copias),
    nunca sobre los fuentes.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

EXIFTOOL_NOT_AVAILABLE = "EXIFTOOL_NOT_AVAILABLE"
OK = "OK"
NOT_COPIED = "NOT_COPIED"
AUDIT_OK = "AUDIT_OK"
AUDIT_MISSING = "AUDIT_MISSING"
AUDIT_MISMATCH = "AUDIT_MISMATCH"
WRITE_FAILED = "WRITE_FAILED"
VERIFY_MISMATCH = "VERIFY_MISMATCH"


def exiftool_binary(config) -> Optional[str]:
    """Devuelve la ruta del binario de ExifTool, o None si no está disponible."""
    explicit = getattr(config, "exiftool_path", None)
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which("exiftool") or shutil.which("exiftool.exe")
    if found:
        return found
    # Instalaciones estándar de Windows que no quedan en PATH (winget per-user, etc.)
    candidates = []
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""),
                 os.environ.get("ProgramFiles(x86)", "")):
        if base:
            candidates.append(os.path.join(base, "Programs", "ExifTool", "ExifTool.exe"))
            candidates.append(os.path.join(base, "ExifTool", "exiftool.exe"))
            candidates.append(os.path.join(base, "ExifTool", "ExifTool.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def _exif_datetime_original(path: Path) -> Optional[datetime]:
    """DateTimeOriginal de la copia usando el lector EXIF del proyecto."""
    try:
        from photos_dedupe.date_utils import _read_exif_datetimes
        for source, naive in _read_exif_datetimes(path):
            if source == "exif_datetime_original":
                return naive
        return None
    except Exception:
        return None


def _audit_copy(copy_path: Path, selected_date_utc: Optional[str]) -> dict:
    """Inspecciona la copia: ¿tiene DateTimeOriginal? ¿coincide con la fecha planificada?"""
    if not copy_path.exists():
        return {"status": NOT_COPIED, "detail": "copia no encontrada"}
    dt_orig = _exif_datetime_original(copy_path)
    result = {"status": AUDIT_MISSING, "detail": "sin DateTimeOriginal"}
    if dt_orig is not None:
        result["status"] = AUDIT_OK
        result["detail"] = dt_orig.strftime("%Y:%m:%d %H:%M:%S")
        result["exif_datetime_original"] = dt_orig.strftime("%Y:%m:%d %H:%M:%S")
        if selected_date_utc:
            try:
                sel = datetime.fromisoformat(selected_date_utc)
                if sel.tzinfo is not None:
                    sel = sel.astimezone().replace(tzinfo=None)
                if sel.strftime("%Y:%m:%d") != dt_orig.strftime("%Y:%m:%d"):
                    result["status"] = AUDIT_MISMATCH
                    result["detail"] = (
                        f"DateTimeOriginal {dt_orig:%Y:%m:%d} != fecha planificada {sel:%Y:%m:%d}"
                    )
            except Exception:
                pass
    return result


def _write_with_exiftool(exiftool: str, copy_path: Path, dt_orig: datetime) -> dict:
    """Escribe DateTimeOriginal con ExifTool en la copia."""
    tag_value = dt_orig.strftime("%Y:%m:%d %H:%M:%S")
    cmd = [exiftool, f"-DateTimeOriginal={tag_value}", "-overwrite_original_in_place", str(copy_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            return {"status": WRITE_FAILED, "detail": (proc.stderr or proc.stdout).strip()[:300]}
        return {"status": OK, "detail": f"DateTimeOriginal={tag_value}"}
    except FileNotFoundError:
        return {"status": EXIFTOOL_NOT_AVAILABLE, "detail": "exiftool no encontrado al ejecutar"}
    except Exception as e:
        return {"status": WRITE_FAILED, "detail": str(e)[:300]}


def apply_metadata(op, config, run_dir: Path) -> dict:
    """
    Ejecuta la acción de metadata planificada para una operación.

    - Se usa op.metadata_action (none | audit | write) definido en el planner.
    - Si metadata_action == "none", devuelve {"status": "none"} sin tocar nada.
    """
    action = getattr(op, "metadata_action", "none")
    if action == "none":
        return {"status": "none"}

    dest = run_dir / op.planned_destination
    if op.status not in ("copied", "moved"):
        return {"status": NOT_COPIED, "detail": "la operación no se ejecutó (dry-run o falló)"}

    if action == "audit":
        res = _audit_copy(dest, op.selected_date)
        logger.info(f"metadata-audit {op.source_path} → {res}")
        return res

    # write
    exiftool = exiftool_binary(config)
    if not exiftool:
        logger.warning(
            "metadata_mode=write pero ExifTool no está en PATH; "
            "no se escribió nada (EXIFTOOL_NOT_AVAILABLE)."
        )
        return {"status": EXIFTOOL_NOT_AVAILABLE, "detail": "exiftool no encontrado en PATH"}

    if not op.selected_date:
        return {"status": "none", "detail": "sin fecha planificada para escribir"}
    try:
        dt = datetime.fromisoformat(op.selected_date)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
    except Exception as e:
        return {"status": WRITE_FAILED, "detail": f"fecha inválida: {e}"}

    res = _write_with_exiftool(exiftool, dest, dt)

    # verificación post-escritura
    if res["status"] == OK and getattr(config, "verify_written_metadata", True):
        written = _exif_datetime_original(dest)
        if written is None:
            res["status"] = VERIFY_MISMATCH
            res["detail"] = "verificación: no se pudo releer DateTimeOriginal"
        elif written.strftime("%Y:%m:%d") != dt.strftime("%Y:%m:%d"):
            res["status"] = VERIFY_MISMATCH
            res["detail"] = f"verificación: leyó {written:%Y:%m:%d %H:%M:%S}, esperaba {dt:%Y:%m:%d}"
    logger.info(f"metadata-write {op.source_path} → {res}")
    return res
