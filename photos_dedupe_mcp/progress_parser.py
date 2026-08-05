from __future__ import annotations

import re
from typing import Any


def parse_progress(lines: list[str]) -> dict[str, Any]:
    """Convierte líneas del proceso y run.log en progreso resumido."""
    clean_lines = [line for line in lines if line.strip()]
    text = "\n".join(clean_lines)
    lowered = text.lower()

    if any(
        token in lowered
        for token in (
            "completado",
            "finalizado",
            "run completed",
            "estado: planned",
        )
    ):
        phase = "completed"
    elif "paso 4/4" in lowered or "reporte" in lowered:
        phase = "reporting"
    elif (
        "paso 3/4" in lowered
        or "planificación" in lowered
        or "planificacion" in lowered
    ):
        phase = "planning"
    elif "phash" in lowered or "hash perceptual" in lowered:
        phase = "perceptual_hashing"
    elif (
        "sha-256" in lowered
        or "sha256" in lowered
        or "hash exact" in lowered
    ):
        phase = "exact_hashing"
    elif "escane" in lowered or "index" in lowered:
        phase = "scanning"
    else:
        phase = "running"

    progress_matches = re.findall(
        r"(?<!\d)(\d{1,9})\s*(?:de|/)\s*(\d{1,9})(?!\d)",
        text,
        flags=re.IGNORECASE,
    )

    processed: int | None = None
    total: int | None = None
    percent: float | None = None

    if progress_matches:
        processed, total = map(int, progress_matches[-1])

        if total > 0 and processed <= total:
            percent = round(processed * 100 / total, 2)

    exact_matches = re.findall(
        r"(\d+)\s+grupos?\s+exact",
        text,
        flags=re.IGNORECASE,
    )
    perceptual_matches = re.findall(
        r"(\d+)\s+(?:grupos?\s+)?perceptual",
        text,
        flags=re.IGNORECASE,
    )
    duplicate_group_matches = re.findall(
        r"(\d+)\s+grupos?\s+duplicad",
        text,
        flags=re.IGNORECASE,
    )

    return {
        "phase": phase,
        "processed": processed,
        "total": total,
        "percent": percent,
        "exact_groups": (
            int(exact_matches[-1])
            if exact_matches
            else None
        ),
        "perceptual_groups": (
            int(perceptual_matches[-1])
            if perceptual_matches
            else None
        ),
        "duplicate_groups": (
            int(duplicate_group_matches[-1])
            if duplicate_group_matches
            else None
        ),
        "last_message": (
            clean_lines[-1]
            if clean_lines
            else None
        ),
    }
