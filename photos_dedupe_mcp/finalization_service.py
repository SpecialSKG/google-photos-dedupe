from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PATH_KEYS = (
    "path",
    "source_path",
    "file_path",
    "original_path",
    "source",
)
DESTINATION_KEYS = (
    "planned_destination",
    "destination_path",
    "target_path",
    "output_path",
    "dest_path",
    "destination",
)
SIZE_KEYS = (
    "size_bytes",
    "file_size",
    "filesize",
    "size",
    "bytes",
)
WIDTH_KEYS = (
    "width",
    "image_width",
    "pixel_x_dimension",
    "exif_image_width",
)
HEIGHT_KEYS = (
    "height",
    "image_height",
    "pixel_y_dimension",
    "exif_image_height",
)
TIME_KEYS = (
    "datetimeoriginal",
    "date_time_original",
    "datetime_original",
    "capture_datetime",
    "taken_at",
    "creation_time",
    "photo_taken_time",
    "timestamp",
    "datetime",
)
CAMERA_KEYS = (
    "camera_model",
    "model",
    "device_model",
)
ALLOWED_APPROVED_ACTIONS = {
    "COPY",
    "KEEP",
    "KEEP_ALL",
    "SKIP_EXACT_DUPLICATE",
    "SKIP_REVIEWED_DUPLICATE",
    "MANUAL_REVIEW",
}
DESTRUCTIVE_WORDS = {"DELETE", "REMOVE", "MOVE", "TRASH", "UNLINK"}


class FinalizationService:
    """Clasifica grupos y genera planes finales sin tocar fotografías."""

    def __init__(self, project_root: Path, review_service: Any) -> None:
        self.project_root = project_root.resolve()
        self.review_service = review_service

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _approved_plan_path(run_dir: Path) -> Path:
        return run_dir / "MANIFESTS" / "approved_plan.jsonl"

    @staticmethod
    def _approved_meta_path(run_dir: Path) -> Path:
        return run_dir / "MANIFESTS" / "approved_plan.meta.json"

    @staticmethod
    def _validation_path(run_dir: Path) -> Path:
        return run_dir / "REPORTS" / "approved_plan_validation.json"

    @staticmethod
    def _processing_plan_path(run_dir: Path) -> Path:
        return run_dir / "MANIFESTS" / "processing_plan.jsonl"

    @staticmethod
    def _normalize_path(value: str) -> str:
        return os.path.normcase(os.path.normpath(value.strip()))

    @staticmethod
    def _extract_path(value: Any, keys: tuple[str, ...] = PATH_KEYS) -> str | None:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None

        if not isinstance(value, dict):
            return None

        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()

        return None

    def _resolve_source(self, value: str | None) -> Path | None:
        if not value:
            return None

        path = Path(value)
        if not path.is_absolute():
            path = self.project_root / path

        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    def _paths_equal(self, first: str | None, second: str | None) -> bool:
        if not first or not second:
            return False
        first_resolved = self._resolve_source(first)
        second_resolved = self._resolve_source(second)
        if first_resolved and second_resolved:
            return self._normalize_path(str(first_resolved)) == self._normalize_path(str(second_resolved))
        return self._normalize_path(first) == self._normalize_path(second)

    @staticmethod
    def _walk_metadata(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
        pairs: list[tuple[str, Any]] = []

        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
                child_prefix = f"{prefix}.{normalized}" if prefix else normalized
                pairs.extend(FinalizationService._walk_metadata(child, child_prefix))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                pairs.extend(FinalizationService._walk_metadata(child, f"{prefix}[{index}]"))
        else:
            pairs.append((prefix, value))

        return pairs

    @staticmethod
    def _find_metadata_value(value: Any, candidate_keys: tuple[str, ...]) -> Any:
        normalized_candidates = {
            re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
            for key in candidate_keys
        }

        for key, candidate in FinalizationService._walk_metadata(value):
            leaf = re.sub(r"[^a-z0-9]+", "_", key.split(".")[-1].lower()).strip("_")
            if leaf in normalized_candidates and candidate not in (None, ""):
                return candidate

        return None

    @staticmethod
    def _parse_int(value: Any) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            match = re.search(r"-?\d+", value.replace(",", ""))
            if match:
                try:
                    return int(match.group(0))
                except ValueError:
                    return None
        return None

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1000.0
            try:
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                return None

        text = str(value).strip()
        if not text:
            return None

        if text.isdigit():
            return FinalizationService._parse_datetime(int(text))

        normalized = text.replace("Z", "+00:00")
        candidates = [
            normalized,
            normalized.replace(":", "-", 2),
        ]
        formats = (
            "%Y:%m:%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S%z",
        )

        for candidate in candidates:
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                pass

            for fmt in formats:
                try:
                    return datetime.strptime(candidate, fmt)
                except ValueError:
                    continue

        return None

    def _member_facts(self, member: dict[str, Any]) -> dict[str, Any]:
        path_value = member.get("path")
        metadata = member.get("metadata") if isinstance(member.get("metadata"), dict) else {}
        resolved = self._resolve_source(path_value if isinstance(path_value, str) else None)

        width = self._parse_int(self._find_metadata_value(metadata, WIDTH_KEYS))
        height = self._parse_int(self._find_metadata_value(metadata, HEIGHT_KEYS))
        size = self._parse_int(self._find_metadata_value(metadata, SIZE_KEYS))
        timestamp_raw = self._find_metadata_value(metadata, TIME_KEYS)
        timestamp = self._parse_datetime(timestamp_raw)
        camera_raw = self._find_metadata_value(metadata, CAMERA_KEYS)
        camera = str(camera_raw).strip() if camera_raw not in (None, "") else None

        if resolved and resolved.is_file():
            try:
                size = resolved.stat().st_size
            except OSError:
                pass

        return {
            "path": path_value,
            "resolved_path": str(resolved) if resolved else None,
            "exists": bool(resolved and resolved.is_file()),
            "size_bytes": size,
            "width": width,
            "height": height,
            "timestamp": timestamp.isoformat() if timestamp else None,
            "timestamp_object": timestamp,
            "camera_model": camera,
        }

    @staticmethod
    def _same_known(values: list[Any]) -> bool:
        known = [value for value in values if value not in (None, "")]
        return len(known) >= 2 and len(set(known)) == 1

    @staticmethod
    def _same_dimensions(facts: list[dict[str, Any]]) -> bool:
        dimensions = [
            (fact.get("width"), fact.get("height"))
            for fact in facts
            if fact.get("width") and fact.get("height")
        ]
        return len(dimensions) >= 2 and len(set(dimensions)) == 1

    @staticmethod
    def _same_aspect_ratio(facts: list[dict[str, Any]]) -> bool:
        ratios: list[float] = []
        for fact in facts:
            width = fact.get("width")
            height = fact.get("height")
            if width and height:
                ratios.append(round(float(width) / float(height), 3))
        return len(ratios) >= 2 and max(ratios) - min(ratios) <= 0.01

    def _classify_group(self, group: dict[str, Any], decision: dict[str, Any] | None) -> dict[str, Any]:
        group_id = group.get("group_id")
        detection_type = str(group.get("detection_type", "")).lower()
        membership = self.review_service.group_members(group)
        facts = [self._member_facts(member) for member in membership["members"]]
        phash_distance = self._parse_int(group.get("phash_distance"))
        evidence: list[str] = []

        timestamps = [fact["timestamp_object"] for fact in facts if fact.get("timestamp_object")]
        time_span_seconds: float | None = None
        if len(timestamps) >= 2:
            time_span_seconds = (max(timestamps) - min(timestamps)).total_seconds()
            evidence.append(f"Diferencia temporal máxima: {time_span_seconds:.1f} s.")

        same_camera = self._same_known([fact.get("camera_model") for fact in facts])
        same_dimensions = self._same_dimensions(facts)
        same_aspect = self._same_aspect_ratio(facts)

        if same_camera:
            evidence.append("Los miembros comparten el mismo modelo de cámara.")
        if same_dimensions:
            evidence.append("Los miembros tienen las mismas dimensiones.")
        elif same_aspect:
            evidence.append("Los miembros conservan la misma relación de aspecto.")
        if phash_distance is not None:
            evidence.append(f"Distancia pHash reportada: {phash_distance}.")

        classification = "uncertain"
        recommended_action = "defer"
        confidence = 0.35
        auto_resolvable = False

        if detection_type == "exact":
            classification = "exact_duplicate"
            recommended_action = "accept_suggested"
            confidence = 1.0
            auto_resolvable = True
            evidence.append("El deduplicador lo clasificó como duplicado exacto.")
        elif (
            time_span_seconds is not None
            and 1.0 <= abs(time_span_seconds) <= 10.0
            and same_dimensions
            and same_camera
        ):
            classification = "burst_sequence"
            recommended_action = "keep_all"
            confidence = 0.9
            evidence.append("La separación temporal corta es compatible con una secuencia o ráfaga.")
        elif (
            time_span_seconds is not None
            and abs(time_span_seconds) <= 1.0
            and same_dimensions
            and phash_distance is not None
            and phash_distance <= 2
        ):
            classification = "same_capture_reencoded"
            recommended_action = "defer"
            confidence = 0.75
            evidence.append("Misma fecha y dimensiones con pHash muy próximo; requiere confirmar calidad visual.")
        elif (
            time_span_seconds is not None
            and abs(time_span_seconds) <= 1.0
            and same_aspect
            and not same_dimensions
        ):
            classification = "edited_version"
            recommended_action = "defer"
            confidence = 0.7
            evidence.append("Mismo instante y relación de aspecto, pero dimensiones diferentes.")
        elif phash_distance is not None and phash_distance <= 4:
            classification = "visually_similar"
            recommended_action = "keep_all"
            confidence = 0.6
            evidence.append("La similitud perceptual es alta, pero los metadatos no prueban que sea la misma captura.")
        else:
            evidence.append("Los metadatos disponibles no permiten una clasificación segura.")

        serializable_facts = []
        for fact in facts:
            clean_fact = dict(fact)
            clean_fact.pop("timestamp_object", None)
            serializable_facts.append(clean_fact)

        return {
            "group_id": group_id,
            "detection_type": detection_type,
            "classification": classification,
            "recommended_action": recommended_action,
            "confidence": round(confidence, 2),
            "auto_resolvable": auto_resolvable,
            "decision_status": self.review_service.decision_status(decision),
            "existing_decision": decision,
            "member_count": len(facts),
            "suggested_winner_path": membership["suggested_winner_path"],
            "phash_distance": phash_distance,
            "time_span_seconds": time_span_seconds,
            "same_camera": same_camera,
            "same_dimensions": same_dimensions,
            "evidence": evidence,
            "member_facts": serializable_facts,
        }

    def classify_groups(
        self,
        run_dir: Path,
        decision_status: str = "pending",
        offset: int = 0,
        limit: int = 50,
        include_paths: bool = False,
    ) -> dict[str, Any]:
        normalized_status = decision_status.strip().lower()
        if normalized_status not in {"all", "pending", "resolved", "deferred"}:
            raise ValueError("decision_status debe ser all, pending, resolved o deferred.")

        safe_offset = max(0, int(offset))
        safe_limit = max(1, min(int(limit), 500))
        groups = self.review_service.load_groups(run_dir)
        decisions, parse_errors = self.review_service.latest_decisions(run_dir)
        matching: list[tuple[dict[str, Any], dict[str, Any] | None]] = []

        for group in groups:
            if str(group.get("detection_type", "")).lower() != "perceptual":
                continue
            group_id = group.get("group_id")
            if not isinstance(group_id, int):
                continue
            decision = decisions.get(group_id)
            status = self.review_service.decision_status(decision)
            if normalized_status != "all" and status != normalized_status:
                continue
            matching.append((group, decision))

        selected = matching[safe_offset:safe_offset + safe_limit]
        items = [self._classify_group(group, decision) for group, decision in selected]

        if not include_paths:
            for item in items:
                for fact in item.get("member_facts", []):
                    fact.pop("path", None)
                    fact.pop("resolved_path", None)
                item.pop("suggested_winner_path", None)

        classification_counts = Counter(item["classification"] for item in items)
        recommendation_counts = Counter(item["recommended_action"] for item in items)

        return {
            "run": run_dir.name,
            "scope": "perceptual_groups_only",
            "decision_status": normalized_status,
            "total_matching": len(matching),
            "offset": safe_offset,
            "limit": safe_limit,
            "returned": len(items),
            "parse_errors": parse_errors,
            "classification_counts": dict(classification_counts),
            "recommendation_counts": dict(recommendation_counts),
            "high_confidence_count": sum(1 for item in items if item["confidence"] >= 0.85),
            "auto_decisions_saved": 0,
            "note": "Las clasificaciones son recomendaciones. No se guardan decisiones ni se modifican archivos.",
            "items": items,
        }

    @staticmethod
    def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
        items: list[dict[str, Any]] = []
        errors = 0
        if not path.is_file():
            raise FileNotFoundError(f"No existe el manifiesto: {path}")

        with path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    item = json.loads(stripped)
                except json.JSONDecodeError:
                    errors += 1
                    continue
                if isinstance(item, dict):
                    item["_source_line"] = line_number
                    items.append(item)
                else:
                    errors += 1
        return items, errors

    @staticmethod
    def _atomic_write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            delete=False,
            suffix=".tmp",
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)

    def _extract_operation_source(self, operation: dict[str, Any]) -> str | None:
        source = self._extract_path(operation, PATH_KEYS)
        if source:
            return source
        for key in ("file", "input", "source_file"):
            nested = operation.get(key)
            source = self._extract_path(nested, PATH_KEYS)
            if source:
                return source
        return None

    def _extract_operation_destination(self, operation: dict[str, Any]) -> str | None:
        destination = self._extract_path(operation, DESTINATION_KEYS)
        if destination:
            return destination
        for key in ("destination", "target", "output"):
            nested = operation.get(key)
            destination = self._extract_path(nested, DESTINATION_KEYS + PATH_KEYS)
            if destination:
                return destination
        return None

    def _source_size(self, operation: dict[str, Any], source_path: str | None) -> int | None:
        size = self._parse_int(self._find_metadata_value(operation, SIZE_KEYS))
        resolved = self._resolve_source(source_path)
        if resolved and resolved.is_file():
            try:
                return resolved.stat().st_size
            except OSError:
                return size
        return size

    def _group_context(self, run_dir: Path) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]], int]:
        groups = self.review_service.load_groups(run_dir)
        decisions, parse_errors = self.review_service.latest_decisions(run_dir)
        group_map = {
            group["group_id"]: group
            for group in groups
            if isinstance(group.get("group_id"), int)
        }
        return group_map, decisions, parse_errors

    def _approved_action_for_operation(
        self,
        operation: dict[str, Any],
        source_path: str | None,
        group: dict[str, Any] | None,
        decision: dict[str, Any] | None,
    ) -> tuple[str, str, dict[str, Any]]:
        bucket = str(operation.get("output_bucket", "")).upper()
        detection_type = str(group.get("detection_type", "")).lower() if group else ""
        selected_path: str | None = None
        decision_id: str | None = None
        review_status = self.review_service.decision_status(decision)

        if group:
            membership = self.review_service.group_members(group)
            suggested = membership["suggested_winner_path"]
            action = str(decision.get("action", "")) if decision else ""
            selected_path = decision.get("selected_path") if decision else None
            decision_id = decision.get("decision_id") if decision else None

            if detection_type == "exact" and not decision:
                selected_path = suggested
                action = "accept_suggested"
                review_status = "auto_exact"

            if action in {"keep_all", "not_duplicates"}:
                return "KEEP_ALL", f"Decisión explícita: {action}.", {
                    "review_status": review_status,
                    "decision_id": decision_id,
                    "selected_path": None,
                }

            if action == "defer" or (detection_type == "perceptual" and not decision):
                return "MANUAL_REVIEW", "Grupo perceptual sin una decisión resolutiva.", {
                    "review_status": review_status,
                    "decision_id": decision_id,
                    "selected_path": None,
                }

            if action in {"accept_suggested", "choose_winner"}:
                selected_path = selected_path or suggested
                if self._paths_equal(source_path, selected_path):
                    return "COPY", "Archivo elegido como representante del grupo.", {
                        "review_status": review_status,
                        "decision_id": decision_id,
                        "selected_path": selected_path,
                    }
                skip_action = (
                    "SKIP_EXACT_DUPLICATE"
                    if detection_type == "exact"
                    else "SKIP_REVIEWED_DUPLICATE"
                )
                return skip_action, "Archivo omitido por una decisión de grupo aprobada.", {
                    "review_status": review_status,
                    "decision_id": decision_id,
                    "selected_path": selected_path,
                }

            return "MANUAL_REVIEW", "El grupo no tiene una decisión compatible.", {
                "review_status": review_status,
                "decision_id": decision_id,
                "selected_path": selected_path,
            }

        if bucket == "UNIQUE":
            return "COPY", "Archivo único según el plan original.", {
                "review_status": "not_required",
                "decision_id": None,
                "selected_path": None,
            }
        if bucket == "REVIEW_DATE":
            return "MANUAL_REVIEW", "La fecha requiere revisión antes de consolidar.", {
                "review_status": "pending_date_review",
                "decision_id": None,
                "selected_path": None,
            }
        if bucket == "REVIEW_PERCEPTUAL":
            return "MANUAL_REVIEW", "Operación perceptual sin grupo o decisión asociada.", {
                "review_status": "pending",
                "decision_id": None,
                "selected_path": None,
            }
        if bucket == "DUPLICATES_EXACT":
            return "MANUAL_REVIEW", "Operación exacta sin grupo asociado; no se infiere el ganador.", {
                "review_status": "incomplete_group_mapping",
                "decision_id": None,
                "selected_path": None,
            }

        return "MANUAL_REVIEW", "Bucket desconocido o incompleto.", {
            "review_status": "unknown",
            "decision_id": None,
            "selected_path": None,
        }

    def build_approved_plan(self, run_dir: Path, overwrite: bool = False) -> dict[str, Any]:
        output_path = self._approved_plan_path(run_dir)
        meta_path = self._approved_meta_path(run_dir)

        if output_path.exists() and not overwrite:
            return {
                "built": False,
                "reason": "approved_plan.jsonl ya existe. Usa overwrite=true para regenerarlo.",
                "run": run_dir.name,
                "approved_plan": str(output_path),
                "metadata_file": str(meta_path),
                "files_modified": [],
            }

        original_path = self._processing_plan_path(run_dir)
        operations, plan_parse_errors = self._read_jsonl(original_path)
        group_map, decisions, decision_parse_errors = self._group_context(run_dir)
        generated_at = self._now()
        approved_records: list[dict[str, Any]] = []
        action_counts: Counter[str] = Counter()
        group_status_counts: Counter[str] = Counter()

        for operation in operations:
            source_line = operation.pop("_source_line", None)
            source_path = self._extract_operation_source(operation)
            destination_path = self._extract_operation_destination(operation)
            group_id = operation.get("group_id")
            group = group_map.get(group_id) if isinstance(group_id, int) else None
            decision = decisions.get(group_id) if isinstance(group_id, int) else None
            approved_action, approved_reason, context = self._approved_action_for_operation(
                operation,
                source_path,
                group,
                decision,
            )
            source_resolved = self._resolve_source(source_path)
            source_size = self._source_size(operation, source_path)
            detection_type = str(group.get("detection_type", "")).lower() if group else None

            record = {
                "approved_plan_version": 1,
                "run_name": run_dir.name,
                "generated_at": generated_at,
                "source_line": source_line,
                "source_path": source_path,
                "resolved_source_path": str(source_resolved) if source_resolved else None,
                "source_exists": bool(source_resolved and source_resolved.is_file()),
                "source_size_bytes": source_size,
                "destination_path": destination_path,
                "output_bucket": operation.get("output_bucket"),
                "group_id": group_id,
                "detection_type": detection_type,
                "approved_action": approved_action,
                "approved_reason": approved_reason,
                "review_status": context["review_status"],
                "decision_id": context["decision_id"],
                "selected_path": context["selected_path"],
                "destructive": False,
                "original_operation": operation,
            }
            approved_records.append(record)
            action_counts[approved_action] += 1
            group_status_counts[str(context["review_status"])] += 1

        serialized_lines = [json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in approved_records]
        content = "\n".join(serialized_lines) + ("\n" if serialized_lines else "")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()

        metadata = {
            "approved_plan_version": 1,
            "run_name": run_dir.name,
            "generated_at": generated_at,
            "source_processing_plan": str(original_path),
            "approved_plan": str(output_path),
            "sha256": digest,
            "operation_count": len(approved_records),
            "action_counts": dict(action_counts),
            "review_status_counts": dict(group_status_counts),
            "latest_decision_count": len(decisions),
            "processing_plan_parse_errors": plan_parse_errors,
            "decision_parse_errors": decision_parse_errors,
            "contains_destructive_operations": False,
            "photos_modified": False,
            "processing_plan_modified": False,
        }

        self._atomic_write_text(output_path, content)
        self._atomic_write_text(meta_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")

        return {
            "built": True,
            "run": run_dir.name,
            "approved_plan": str(output_path),
            "metadata_file": str(meta_path),
            "sha256": digest,
            "operation_count": len(approved_records),
            "action_counts": dict(action_counts),
            "manual_review_count": action_counts["MANUAL_REVIEW"],
            "latest_decision_count": len(decisions),
            "parse_errors": {
                "processing_plan": plan_parse_errors,
                "review_decisions": decision_parse_errors,
            },
            "files_modified": [str(output_path), str(meta_path)],
            "photos_modified": False,
            "processing_plan_modified": False,
        }

    def validate_approved_plan(self, run_dir: Path, write_report: bool = True) -> dict[str, Any]:
        approved_path = self._approved_plan_path(run_dir)
        meta_path = self._approved_meta_path(run_dir)
        records, parse_errors = self._read_jsonl(approved_path)
        errors: list[str] = []
        warnings: list[str] = []
        action_counts: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        destination_counts: Counter[str] = Counter()
        groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
        missing_sources = 0
        exact_savings = 0
        reviewed_savings = 0

        for record in records:
            record.pop("_source_line", None)
            action = str(record.get("approved_action", "")).upper()
            action_counts[action] += 1

            if action not in ALLOWED_APPROVED_ACTIONS:
                errors.append(f"Acción no admitida: {action or '<vacía>'}.")

            serialized_upper = json.dumps(record, ensure_ascii=False).upper()
            if any(word in serialized_upper for word in DESTRUCTIVE_WORDS) and record.get("destructive"):
                errors.append("Se detectó una operación marcada como destructiva.")

            source_path = record.get("source_path")
            if isinstance(source_path, str) and source_path.strip():
                normalized_source = self._normalize_path(source_path)
                source_counts[normalized_source] += 1
                resolved = self._resolve_source(source_path)
                if not resolved or not resolved.is_file():
                    missing_sources += 1
                    if missing_sources <= 100:
                        errors.append(f"No existe el archivo fuente: {source_path}")
            else:
                errors.append("Una operación no contiene source_path.")

            destination_path = record.get("destination_path")
            if action in {"COPY", "KEEP", "KEEP_ALL"}:
                if isinstance(destination_path, str) and destination_path.strip():
                    destination_counts[self._normalize_path(destination_path)] += 1
                else:
                    errors.append(
                        f"La operación {action} no contiene destination_path: "
                        f"{source_path or '<sin source_path>'}"
                    )

            size = self._parse_int(record.get("source_size_bytes")) or 0
            if action == "SKIP_EXACT_DUPLICATE":
                exact_savings += size
            elif action == "SKIP_REVIEWED_DUPLICATE":
                reviewed_savings += size

            group_id = record.get("group_id")
            # group_id=-1 identifica operaciones independientes (UNIQUE y
            # REVIEW_DATE), no un grupo de duplicados. Solo los IDs no
            # negativos representan grupos reales y deben validarse juntos.
            if isinstance(group_id, int) and group_id >= 0:
                groups[group_id].append(record)

        duplicate_sources = [path for path, count in source_counts.items() if count > 1]
        for path in duplicate_sources[:100]:
            errors.append(f"La ruta fuente aparece más de una vez: {path}")

        destination_collisions = [path for path, count in destination_counts.items() if count > 1]
        for path in destination_collisions[:100]:
            errors.append(f"Colisión de destino: {path}")

        for group_id, group_records in groups.items():
            detection_types = {str(item.get("detection_type", "")).lower() for item in group_records}
            actions = [str(item.get("approved_action", "")).upper() for item in group_records]
            action_set = set(actions)

            if "MANUAL_REVIEW" in action_set:
                if action_set != {"MANUAL_REVIEW"}:
                    errors.append(f"Grupo {group_id}: mezcla MANUAL_REVIEW con acciones finales.")
                continue

            if action_set == {"KEEP_ALL"}:
                continue

            copy_count = actions.count("COPY") + actions.count("KEEP")
            if "exact" in detection_types:
                if copy_count != 1 or any(action not in {"COPY", "KEEP", "SKIP_EXACT_DUPLICATE"} for action in actions):
                    errors.append(f"Grupo exacto {group_id}: debe conservar exactamente un representante y omitir el resto como exactos.")
            elif "perceptual" in detection_types:
                if copy_count != 1 or any(action not in {"COPY", "KEEP", "SKIP_REVIEWED_DUPLICATE"} for action in actions):
                    errors.append(f"Grupo perceptual {group_id}: la decisión aprobada debe conservar exactamente un representante o KEEP_ALL.")

        metadata: dict[str, Any] | None = None
        if meta_path.is_file():
            try:
                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                expected_hash = metadata.get("sha256") if isinstance(metadata, dict) else None
                actual_hash = hashlib.sha256(approved_path.read_bytes()).hexdigest()
                if expected_hash and expected_hash != actual_hash:
                    errors.append("El hash del approved_plan no coincide con approved_plan.meta.json.")
            except (OSError, json.JSONDecodeError):
                errors.append("No se pudo leer approved_plan.meta.json.")
        else:
            warnings.append("No existe approved_plan.meta.json.")

        if parse_errors:
            errors.append(f"El plan contiene {parse_errors} líneas JSON inválidas.")

        manual_review_count = action_counts["MANUAL_REVIEW"]
        ready = not errors and manual_review_count == 0
        if manual_review_count:
            warnings.append(f"Quedan {manual_review_count} operaciones en MANUAL_REVIEW.")

        result = {
            "valid": not errors,
            "ready_for_execution": ready,
            "run": run_dir.name,
            "approved_plan": str(approved_path),
            "metadata_file": str(meta_path),
            "validation_report": str(self._validation_path(run_dir)),
            "operation_count": len(records),
            "action_counts": dict(action_counts),
            "group_count": len(groups),
            "manual_review_count": manual_review_count,
            "missing_source_count": missing_sources,
            "duplicate_source_count": len(duplicate_sources),
            "destination_collision_count": len(destination_collisions),
            "parse_errors": parse_errors,
            "estimated_savings": {
                "exact_bytes": exact_savings,
                "reviewed_bytes": reviewed_savings,
                "total_bytes": exact_savings + reviewed_savings,
            },
            "errors": errors[:200],
            "warnings": warnings[:200],
            "photos_modified": False,
            "processing_plan_modified": False,
        }

        if write_report:
            self._atomic_write_text(
                self._validation_path(run_dir),
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            )

        return result
