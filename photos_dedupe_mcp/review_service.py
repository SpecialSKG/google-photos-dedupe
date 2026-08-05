from __future__ import annotations

import json
import os
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_ACTIONS = {
    "accept_suggested",
    "choose_winner",
    "keep_all",
    "not_duplicates",
    "defer",
}
RESOLVED_ACTIONS = {
    "accept_suggested",
    "choose_winner",
    "keep_all",
    "not_duplicates",
}
PATH_KEYS = (
    "path",
    "source_path",
    "file_path",
    "original_path",
    "source",
)


class ReviewService:
    """Guarda decisiones de revisión sin modificar fotografías ni planes."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _decision_path(run_dir: Path) -> Path:
        return run_dir / "MANIFESTS" / "review_decisions.jsonl"

    @staticmethod
    def _report_path(run_dir: Path) -> Path:
        return run_dir / "REPORTS" / "dedupe_report.json"

    @staticmethod
    def _normalize_path(value: str) -> str:
        return os.path.normcase(os.path.normpath(value.strip()))

    @staticmethod
    def _extract_path(value: Any) -> str | None:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None

        if not isinstance(value, dict):
            return None

        for key in PATH_KEYS:
            candidate = value.get(key)

            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        return None

    def _load_report(self, run_dir: Path) -> list[dict[str, Any]]:
        report_path = self._report_path(run_dir)

        if not report_path.is_file():
            raise FileNotFoundError(
                f"No existe el reporte de duplicados: {report_path}"
            )

        report = json.loads(report_path.read_text(encoding="utf-8"))

        if not isinstance(report, list):
            raise ValueError(
                "dedupe_report.json no contiene una lista válida."
            )

        return [item for item in report if isinstance(item, dict)]

    @staticmethod
    def _find_group(
        report: list[dict[str, Any]],
        group_id: int,
    ) -> dict[str, Any]:
        for group in report:
            if group.get("group_id") == group_id:
                return group

        raise ValueError(f"No existe el grupo {group_id} en el reporte.")

    def _group_members(self, group: dict[str, Any]) -> dict[str, Any]:
        winner_value = group.get("winner")
        suggested_winner = self._extract_path(winner_value)

        members: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_member(value: Any, role: str) -> None:
            path = self._extract_path(value)

            if not path:
                return

            normalized = self._normalize_path(path)

            if normalized in seen:
                return

            seen.add(normalized)
            members.append(
                {
                    "path": path,
                    "role": role,
                    "metadata": value if isinstance(value, dict) else {},
                }
            )

        add_member(winner_value, "suggested_winner")

        duplicates = group.get("duplicates")
        if isinstance(duplicates, list):
            for duplicate in duplicates:
                add_member(duplicate, "duplicate")

        # Compatibilidad con reportes que usan una lista genérica.
        files = group.get("files")
        if isinstance(files, list):
            for file_value in files:
                add_member(file_value, "member")

        return {
            "suggested_winner_path": suggested_winner,
            "members": members,
            "member_paths": [member["path"] for member in members],
        }

    # API interna estable para los servicios de clasificación/finalización.
    def load_groups(self, run_dir: Path) -> list[dict[str, Any]]:
        return self._load_report(run_dir)

    def group_members(self, group: dict[str, Any]) -> dict[str, Any]:
        return self._group_members(group)

    def latest_decisions(
        self,
        run_dir: Path,
    ) -> tuple[dict[int, dict[str, Any]], int]:
        return self._read_latest_decisions(run_dir)

    @staticmethod
    def decision_status(decision: dict[str, Any] | None) -> str:
        return ReviewService._decision_status(decision)

    def _read_latest_decisions(
        self,
        run_dir: Path,
    ) -> tuple[dict[int, dict[str, Any]], int]:
        path = self._decision_path(run_dir)
        latest: dict[int, dict[str, Any]] = {}
        parse_errors = 0

        if not path.is_file():
            return latest, parse_errors

        with path.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()

                if not line:
                    continue

                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    parse_errors += 1
                    continue

                if not isinstance(record, dict):
                    parse_errors += 1
                    continue

                group_id = record.get("group_id")
                if isinstance(group_id, int):
                    latest[group_id] = record

        return latest, parse_errors

    @staticmethod
    def _decision_status(
        decision: dict[str, Any] | None,
    ) -> str:
        if not decision:
            return "pending"

        action = str(decision.get("action", ""))

        if action == "defer":
            return "deferred"

        if action in RESOLVED_ACTIONS:
            return "resolved"

        return "pending"

    def list_queue(
        self,
        run_dir: Path,
        review_type: str = "perceptual",
        decision_status: str = "pending",
        offset: int = 0,
        limit: int = 20,
        include_paths: bool = False,
    ) -> dict[str, Any]:
        normalized_type = review_type.strip().lower()
        normalized_status = decision_status.strip().lower()

        if normalized_type not in {"all", "exact", "perceptual"}:
            raise ValueError(
                "review_type debe ser all, exact o perceptual."
            )

        if normalized_status not in {
            "all",
            "pending",
            "resolved",
            "deferred",
        }:
            raise ValueError(
                "decision_status debe ser all, pending, resolved o deferred."
            )

        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(int(limit), 100))
        report = self._load_report(run_dir)
        decisions, parse_errors = self._read_latest_decisions(run_dir)
        queue: list[dict[str, Any]] = []

        for group in report:
            detection_type = str(
                group.get("detection_type", "")
            ).lower()

            if normalized_type != "all" and detection_type != normalized_type:
                continue

            group_id = group.get("group_id")
            if not isinstance(group_id, int):
                continue

            decision = decisions.get(group_id)
            status = self._decision_status(decision)

            if normalized_status != "all" and status != normalized_status:
                continue

            membership = self._group_members(group)
            item: dict[str, Any] = {
                "group_id": group_id,
                "detection_type": detection_type,
                "decision_status": status,
                "member_count": len(membership["member_paths"]),
                "suggested_winner_path": membership[
                    "suggested_winner_path"
                ],
                "phash_distance": group.get("phash_distance"),
                "reason": group.get("reason"),
                "latest_decision": decision,
            }

            if include_paths:
                item["member_paths"] = membership["member_paths"]

            queue.append(item)

        selected = queue[safe_offset:safe_offset + safe_limit]

        return {
            "run": run_dir.name,
            "review_type": normalized_type,
            "decision_status": normalized_status,
            "decisions_file": str(self._decision_path(run_dir)),
            "total_matching": len(queue),
            "offset": safe_offset,
            "limit": safe_limit,
            "returned": len(selected),
            "parse_errors": parse_errors,
            "items": selected,
        }

    def get_decision(
        self,
        run_dir: Path,
        group_id: int,
    ) -> dict[str, Any]:
        report = self._load_report(run_dir)
        group = self._find_group(report, group_id)
        decisions, parse_errors = self._read_latest_decisions(run_dir)
        decision = decisions.get(group_id)
        membership = self._group_members(group)

        return {
            "found": True,
            "run": run_dir.name,
            "group_id": group_id,
            "detection_type": group.get("detection_type"),
            "decision_status": self._decision_status(decision),
            "latest_decision": decision,
            "suggested_winner_path": membership[
                "suggested_winner_path"
            ],
            "member_paths": membership["member_paths"],
            "parse_errors": parse_errors,
        }

    def save_decision(
        self,
        run_dir: Path,
        group_id: int,
        action: str,
        selected_path: str = "",
        note: str = "",
        reviewer: str = "user",
    ) -> dict[str, Any]:
        normalized_action = action.strip().lower()

        if normalized_action not in ALLOWED_ACTIONS:
            raise ValueError(
                "action debe ser accept_suggested, choose_winner, "
                "keep_all, not_duplicates o defer."
            )

        report = self._load_report(run_dir)
        group = self._find_group(report, group_id)
        membership = self._group_members(group)
        suggested = membership["suggested_winner_path"]
        member_paths = membership["member_paths"]
        selected = selected_path.strip()

        if normalized_action == "accept_suggested":
            if not suggested:
                raise ValueError(
                    "El grupo no tiene un ganador sugerido utilizable."
                )
            selected = suggested

        elif normalized_action == "choose_winner":
            if not selected:
                raise ValueError(
                    "choose_winner requiere selected_path."
                )

            allowed = {
                self._normalize_path(path): path
                for path in member_paths
            }
            normalized_selected = self._normalize_path(selected)

            if normalized_selected not in allowed:
                raise ValueError(
                    "selected_path no pertenece al grupo. Consulta "
                    "get_review_decision para obtener las rutas permitidas."
                )

            selected = allowed[normalized_selected]

        else:
            selected = ""

        latest, _ = self._read_latest_decisions(run_dir)
        previous = latest.get(group_id)
        decisions_path = self._decision_path(run_dir)
        decisions_path.parent.mkdir(parents=True, exist_ok=True)

        record = {
            "decision_id": uuid.uuid4().hex,
            "run_name": run_dir.name,
            "group_id": group_id,
            "detection_type": str(
                group.get("detection_type", "")
            ).lower(),
            "action": normalized_action,
            "status": self._decision_status(
                {"action": normalized_action}
            ),
            "selected_path": selected or None,
            "suggested_winner_path": suggested,
            "member_count": len(member_paths),
            "note": note.strip()[:4000],
            "reviewer": reviewer.strip()[:200] or "user",
            "created_at": self._now(),
            "supersedes_decision_id": (
                previous.get("decision_id")
                if previous
                else None
            ),
        }

        serialized = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self._lock:
            with decisions_path.open("a", encoding="utf-8") as file:
                file.write(serialized + "\n")
                file.flush()
                os.fsync(file.fileno())

        return {
            "saved": True,
            "run": run_dir.name,
            "decisions_file": str(decisions_path),
            "decision": record,
            "previous_decision": previous,
            "files_modified": [],
            "processing_plan_modified": False,
        }

    def progress(self, run_dir: Path) -> dict[str, Any]:
        report = self._load_report(run_dir)
        decisions, parse_errors = self._read_latest_decisions(run_dir)

        type_counts: Counter[str] = Counter()
        overall_status: Counter[str] = Counter()
        status_by_type: dict[str, Counter[str]] = {
            "exact": Counter(),
            "perceptual": Counter(),
        }
        action_counts: Counter[str] = Counter()

        for group in report:
            group_id = group.get("group_id")
            if not isinstance(group_id, int):
                continue

            detection_type = str(
                group.get("detection_type", "unknown")
            ).lower()
            type_counts[detection_type] += 1

            decision = decisions.get(group_id)
            status = self._decision_status(decision)
            overall_status[status] += 1
            status_by_type.setdefault(detection_type, Counter())[status] += 1

            if decision:
                action_counts[str(decision.get("action", "unknown"))] += 1

        total_groups = sum(type_counts.values())
        overall_resolved = overall_status["resolved"]
        overall_percent = (
            round(overall_resolved * 100 / total_groups, 2)
            if total_groups
            else 0.0
        )

        manual_total = type_counts["perceptual"]
        manual_status = status_by_type.get("perceptual", Counter())
        manual_resolved = manual_status["resolved"]
        manual_percent = (
            round(manual_resolved * 100 / manual_total, 2)
            if manual_total
            else 0.0
        )

        exact_total = type_counts["exact"]
        exact_status = status_by_type.get("exact", Counter())

        return {
            "run": run_dir.name,
            "decisions_file": str(self._decision_path(run_dir)),
            "total_groups": total_groups,
            "groups_by_type": dict(type_counts),
            "groups_by_status": dict(overall_status),
            "groups_by_type_and_status": {
                key: dict(value)
                for key, value in status_by_type.items()
            },
            "decisions_by_action": dict(action_counts),
            # Compatibilidad: este porcentaje representa ahora la cola manual real.
            "resolved_percent": manual_percent,
            "overall_resolved_percent": overall_percent,
            "manual_review": {
                "scope": "perceptual",
                "total_groups": manual_total,
                "pending_groups": manual_status["pending"],
                "resolved_groups": manual_resolved,
                "deferred_groups": manual_status["deferred"],
                "resolved_percent": manual_percent,
            },
            "exact_groups": {
                "total_groups": exact_total,
                "pending_groups": exact_status["pending"],
                "resolved_groups": exact_status["resolved"],
                "deferred_groups": exact_status["deferred"],
                "manual_review_required": False,
                "note": "Los exactos pueden resolverse de forma determinista al construir el plan aprobado.",
            },
            "latest_decision_count": len(decisions),
            "parse_errors": parse_errors,
            "date_review_supported": False,
            "note": (
                "El porcentaje principal excluye los grupos exactos y mide "
                "solo la revisión manual perceptual. REVIEW_DATE se valida "
                "por separado en el plan aprobado."
            ),
        }

