from __future__ import annotations

import importlib.metadata
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from photos_dedupe.config import Config

from photos_dedupe_mcp.job_manager import JobManager
from photos_dedupe_mcp.progress_parser import parse_progress
from photos_dedupe_mcp.review_service import ReviewService
from photos_dedupe_mcp.finalization_service import FinalizationService


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_CANDIDATES = (
    PROJECT_ROOT / "config.yaml",
    PROJECT_ROOT / "config.yml",
)
MAX_RESULT_ITEMS = 500

mcp = MCPServer("Google Photos Dedupe")
JOB_MANAGER = JobManager(PROJECT_ROOT)
REVIEW_SERVICE = ReviewService(PROJECT_ROOT)
FINALIZATION_SERVICE = FinalizationService(PROJECT_ROOT, REVIEW_SERVICE)


# ---------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------

def find_config() -> Path | None:
    """Encuentra config.yaml o config.yml en la raíz."""
    for candidate in CONFIG_CANDIDATES:
        if candidate.is_file():
            return candidate

    return None


def resolve_project_path(value: str | Path) -> Path:
    """Resuelve rutas relativas tomando como base la raíz del proyecto."""
    path = Path(value)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path.resolve()


def load_project_config() -> tuple[Config, Path]:
    """Carga la configuración usando la clase real del proyecto."""
    config_path = find_config()

    if config_path is None:
        raise FileNotFoundError(
            "No se encontró config.yaml ni config.yml."
        )

    config = Config()
    config.load_from_file(str(config_path))
    config.validate()

    return config, config_path


def get_output_directory(config: Config) -> Path:
    """Obtiene el out_dir real de la configuración."""
    return resolve_project_path(config.out_dir)


def get_run_directories(config: Config) -> list[Path]:
    """Devuelve las ejecuciones run_* ordenadas cronológicamente."""
    output_dir = get_output_directory(config)

    if not output_dir.is_dir():
        return []

    return sorted(
        (
            path
            for path in output_dir.iterdir()
            if path.is_dir() and path.name.startswith("run_")
        ),
        key=lambda path: path.name,
    )


def select_run(
    config: Config,
    run_name: str | None = None,
) -> Path:
    """Selecciona una ejecución concreta o la más reciente."""
    output_dir = get_output_directory(config)

    if run_name:
        if Path(run_name).name != run_name:
            raise ValueError(
                "run_name debe ser únicamente el nombre de la carpeta."
            )

        if not run_name.startswith("run_"):
            raise ValueError(
                "La carpeta solicitada debe comenzar con run_."
            )

        candidate = (output_dir / run_name).resolve()
        output_resolved = output_dir.resolve()

        if (
            candidate != output_resolved
            and output_resolved not in candidate.parents
        ):
            raise ValueError(
                "La ejecución solicitada está fuera de out_dir."
            )

        if not candidate.is_dir():
            raise FileNotFoundError(
                f"No existe la ejecución: {run_name}"
            )

        return candidate

    runs = get_run_directories(config)

    if not runs:
        raise FileNotFoundError(
            "No existen ejecuciones run_* dentro de out_dir."
        )

    return runs[-1]


def read_json_file(path: Path) -> Any:
    """Lee un archivo JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def clamp_limit(limit: int) -> int:
    """Evita respuestas excesivamente grandes."""
    return max(1, min(int(limit), MAX_RESULT_ITEMS))


def validate_config_data() -> dict[str, Any]:
    """Valida configuración, entradas y ubicación de salida."""
    errors: list[str] = []
    warnings: list[str] = []

    try:
        config, config_path = load_project_config()
    except Exception as error:
        return {
            "valid": False,
            "errors": [str(error)],
            "warnings": [],
        }

    input_details: list[dict[str, Any]] = []

    for raw_input in config.inputs:
        input_path = resolve_project_path(raw_input)
        exists = input_path.exists()
        is_directory = input_path.is_dir()

        input_details.append(
            {
                "configured_path": str(raw_input),
                "resolved_path": str(input_path),
                "exists": exists,
                "is_directory": is_directory,
            }
        )

        if not exists:
            errors.append(
                f"No existe el input configurado: {input_path}"
            )
        elif not is_directory:
            errors.append(
                f"El input no es una carpeta: {input_path}"
            )

    output_dir = get_output_directory(config)

    for input_detail in input_details:
        input_path = Path(input_detail["resolved_path"])

        if output_dir == input_path or input_path in output_dir.parents:
            errors.append(
                "out_dir está dentro de un input. "
                "El programa podría volver a escanear sus resultados: "
                f"{output_dir}"
            )

    if config.action == "move":
        warnings.append(
            "config.yaml tiene action=move. El MCP solo puede iniciar "
            "dry-run y no ejecutará esa acción destructiva."
        )

    return {
        "valid": not errors,
        "config_file": str(config_path),
        "errors": errors,
        "warnings": warnings,
        "inputs": input_details,
        "output_directory": str(output_dir),
        "settings": {
            "mode": config.mode,
            "configured_action": config.action,
            "phash_threshold": config.phash_threshold,
            "workers": config.workers,
            "group_by_year": config.group_by_year,
            "perceptual_policy": config.perceptual_policy,
            "metadata_mode": config.metadata_mode,
            "use_hash_cache": config.use_hash_cache,
        },
    }


def read_plan_operations(
    run_dir: Path,
    bucket: str | None = None,
    requires_review: str = "",
    group_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Lee processing_plan.jsonl aplicando filtros y paginación."""
    plan_path = run_dir / "MANIFESTS" / "processing_plan.jsonl"

    if not plan_path.is_file():
        raise FileNotFoundError(
            f"No existe el manifiesto: {plan_path}"
        )

    safe_offset = max(0, int(offset))
    safe_limit = clamp_limit(limit)

    items: list[dict[str, Any]] = []
    total_matching = 0
    parse_errors = 0

    with plan_path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                operation = json.loads(line)
            except json.JSONDecodeError:
                parse_errors += 1
                continue

            if bucket and operation.get("output_bucket") != bucket:
                continue

            if (
                requires_review is not None
                and bool(operation.get("requires_review"))
                is not requires_review
            ):
                continue

            if (
                group_id is not None
                and operation.get("group_id") != group_id
            ):
                continue

            if (
                total_matching >= safe_offset
                and len(items) < safe_limit
            ):
                operation["_line_number"] = line_number
                items.append(operation)

            total_matching += 1

    return {
        "run": run_dir.name,
        "manifest": str(plan_path),
        "total_matching": total_matching,
        "offset": safe_offset,
        "limit": safe_limit,
        "returned": len(items),
        "parse_errors": parse_errors,
        "items": items,
    }


def summarize_group(group: dict[str, Any]) -> dict[str, Any]:
    """Reduce un grupo para evitar respuestas MCP demasiado grandes."""
    duplicates = group.get("duplicates")

    if not isinstance(duplicates, list):
        duplicates = []

    winner = group.get("winner")
    file_count = len(duplicates) + (1 if winner else 0)

    return {
        "group_id": group.get("group_id"),
        "detection_type": group.get("detection_type"),
        "file_count": file_count,
        "winner": winner,
        "phash_distance": group.get("phash_distance"),
        "reason": group.get("reason"),
        "requires_review": (
            str(group.get("detection_type", "")).lower()
            == "perceptual"
        ),
    }


def read_run_log_lines(
    output_directory: str,
    run_name: str | None,
    lines: int,
) -> list[str]:
    """Lee el run.log asociado a un trabajo, cuando ya existe."""
    if not run_name:
        return []

    log_path = (
        Path(output_directory)
        / run_name
        / "LOGS"
        / "run.log"
    )

    if not log_path.is_file():
        return []

    safe_lines = max(10, min(int(lines), 300))

    return log_path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()[-safe_lines:]


# ---------------------------------------------------------------------
# Herramientas MCP: proyecto y configuración
# ---------------------------------------------------------------------

@mcp.tool()
def get_project_status() -> dict[str, Any]:
    """Muestra el estado general del proyecto y del MCP."""
    validation = validate_config_data()

    try:
        package_version = importlib.metadata.version(
            "google-photos-dedupe"
        )
    except importlib.metadata.PackageNotFoundError:
        package_version = None

    runs: list[Path] = []

    if validation.get("valid"):
        try:
            config, _ = load_project_config()
            runs = get_run_directories(config)
        except Exception:
            runs = []

    active_job = JOB_MANAGER.get_active_job()

    return {
        "project_root": str(PROJECT_ROOT),
        "package_version": package_version,
        "config_valid": validation.get("valid", False),
        "config_file": validation.get("config_file"),
        "output_directory": validation.get("output_directory"),
        "run_count": len(runs),
        "latest_run": str(runs[-1]) if runs else None,
        "active_dry_run": active_job,
        "available_tools": [
            "get_project_status",
            "validate_config",
            "start_dry_run",
            "get_active_dry_run",
            "get_dry_run_status",
            "get_dry_run_progress",
            "list_dry_run_jobs",
            "cancel_dry_run",
            "list_runs",
            "get_run_summary",
            "list_duplicate_groups",
            "get_group_details",
            "list_plan_operations",
            "list_review_queue",
            "get_review_progress",
            "get_review_decision",
            "save_review_decision",
            "reopen_review_group",
            "classify_review_groups",
            "build_approved_plan",
            "validate_approved_plan",
            "read_run_errors",
        ],
    }


@mcp.tool()
def validate_config() -> dict[str, Any]:
    """Valida config.yaml mediante la configuración real del proyecto."""
    return validate_config_data()


# ---------------------------------------------------------------------
# Herramientas MCP: dry-run no bloqueante
# ---------------------------------------------------------------------

@mcp.tool()
def start_dry_run() -> dict[str, Any]:
    """Inicia un dry-run y devuelve inmediatamente un job_id."""
    validation = validate_config_data()

    if not validation["valid"]:
        return {
            "started": False,
            "reason": "La configuración no es válida.",
            "validation": validation,
        }

    config, config_path = load_project_config()

    return JOB_MANAGER.start_dry_run(
        config_path=config_path,
        output_directory=get_output_directory(config),
    )


@mcp.tool()
def get_active_dry_run() -> dict[str, Any]:
    """Devuelve el dry-run activo, si existe."""
    active = JOB_MANAGER.get_active_job()

    return {
        "active": active is not None,
        "job": active,
    }


@mcp.tool()
def get_dry_run_status(
    job_id: str,
) -> dict[str, Any]:
    """Consulta el estado actual de un dry-run."""
    return JOB_MANAGER.get_status(job_id)


@mcp.tool()
def get_dry_run_progress(
    job_id: str,
    lines: int = 60,
) -> dict[str, Any]:
    """Devuelve estado, progreso interpretado y últimas líneas."""
    status = JOB_MANAGER.get_status(job_id)
    logs = JOB_MANAGER.read_logs(job_id, lines)
    run_log = read_run_log_lines(
        output_directory=status["output_directory"],
        run_name=status.get("run_name"),
        lines=lines,
    )

    combined_lines = (
        logs["stdout"]
        + logs["stderr"]
        + run_log
    )
    progress = parse_progress(combined_lines)

    if status["status"] == "completed":
        progress["phase"] = "completed"
        progress["percent"] = 100.0

    return {
        "status": status,
        "progress": progress,
        "run_log": run_log,
        "process_stdout": logs["stdout"],
        "process_stderr": logs["stderr"],
        "poll_after_seconds": (
            20 if status["status"] == "running" else None
        ),
    }


@mcp.tool()
def list_dry_run_jobs(
    limit: int = 20,
) -> dict[str, Any]:
    """Lista trabajos persistidos e incluye siempre el run asociado."""
    result = JOB_MANAGER.list_jobs(limit)

    for job in result.get("jobs", []):
        job["run_associated"] = job.get("run_name")

    return result


@mcp.tool()
def cancel_dry_run(
    job_id: str,
) -> dict[str, Any]:
    """Cancela el dry-run y sus procesos hijos."""
    return JOB_MANAGER.cancel(job_id)


# ---------------------------------------------------------------------
# Herramientas MCP: ejecuciones y reportes
# ---------------------------------------------------------------------

@mcp.tool()
def list_runs(
    limit: int = 20,
) -> dict[str, Any]:
    """Lista las ejecuciones run_* disponibles."""
    config, _ = load_project_config()
    runs = get_run_directories(config)
    safe_limit = clamp_limit(limit)
    selected = list(reversed(runs))[:safe_limit]

    return {
        "output_directory": str(get_output_directory(config)),
        "total_runs": len(runs),
        "returned": len(selected),
        "runs": [
            {
                "name": run.name,
                "path": str(run),
                "has_summary": (
                    run / "REPORTS" / "run_summary.txt"
                ).is_file(),
                "has_json_report": (
                    run / "REPORTS" / "dedupe_report.json"
                ).is_file(),
                "has_plan": (
                    run
                    / "MANIFESTS"
                    / "processing_plan.jsonl"
                ).is_file(),
                "has_state": (
                    run / "MANIFESTS" / "run_state.json"
                ).is_file(),
                "has_log": (
                    run / "LOGS" / "run.log"
                ).is_file(),
            }
            for run in selected
        ],
    }


@mcp.tool()
def get_run_summary(
    run_name: str = "",
) -> dict[str, Any]:
    """Obtiene el resumen y estado de una ejecución."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)

    summary_path = run_dir / "REPORTS" / "run_summary.txt"
    state_path = run_dir / "MANIFESTS" / "run_state.json"

    result: dict[str, Any] = {
        "run": run_dir.name,
        "path": str(run_dir),
        "summary_file": str(summary_path),
        "state_file": str(state_path),
        "summary_exists": summary_path.is_file(),
        "state_exists": state_path.is_file(),
    }

    if summary_path.is_file():
        result["summary_text"] = summary_path.read_text(
            encoding="utf-8",
            errors="replace",
        )[-50000:]

    if state_path.is_file():
        result["state"] = read_json_file(state_path)

    return result


@mcp.tool()
def list_duplicate_groups(
    detection_type: str = "all",
    run_name: str = "",
    offset: int = 0,
    limit: int = 20,
    include_files: bool = False,
) -> dict[str, Any]:
    """Lista grupos exactos, perceptuales o ambos."""
    normalized_type = detection_type.strip().lower()

    if normalized_type not in {
        "all",
        "exact",
        "perceptual",
    }:
        raise ValueError(
            "detection_type debe ser all, exact o perceptual."
        )

    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)
    report_path = run_dir / "REPORTS" / "dedupe_report.json"

    if not report_path.is_file():
        raise FileNotFoundError(
            f"No existe el reporte JSON: {report_path}"
        )

    report = read_json_file(report_path)

    if not isinstance(report, list):
        raise ValueError(
            "dedupe_report.json no contiene una lista válida."
        )

    filtered = [
        group
        for group in report
        if (
            normalized_type == "all"
            or str(group.get("detection_type", "")).lower()
            == normalized_type
        )
    ]

    safe_offset = max(0, int(offset))
    safe_limit = clamp_limit(limit)
    selected = filtered[
        safe_offset:safe_offset + safe_limit
    ]

    groups = (
        selected
        if include_files
        else [summarize_group(group) for group in selected]
    )

    return {
        "run": run_dir.name,
        "report": str(report_path),
        "detection_type": normalized_type,
        "include_files": include_files,
        "total_matching": len(filtered),
        "offset": safe_offset,
        "limit": safe_limit,
        "returned": len(groups),
        "groups": groups,
    }


@mcp.tool()
def get_group_details(
    group_id: int,
    run_name: str = "",
) -> dict[str, Any]:
    """Obtiene un grupo y sus operaciones planificadas."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)
    report_path = run_dir / "REPORTS" / "dedupe_report.json"

    if not report_path.is_file():
        raise FileNotFoundError(
            f"No existe el reporte JSON: {report_path}"
        )

    report = read_json_file(report_path)
    selected_group = None

    for group in report:
        if group.get("group_id") == group_id:
            selected_group = group
            break

    if selected_group is None:
        return {
            "found": False,
            "run": run_dir.name,
            "group_id": group_id,
        }

    operations = read_plan_operations(
        run_dir=run_dir,
        group_id=group_id,
        offset=0,
        limit=MAX_RESULT_ITEMS,
    )

    return {
        "found": True,
        "run": run_dir.name,
        "group": selected_group,
        "plan_operations": operations["items"],
        "operation_count": operations["total_matching"],
    }


@mcp.tool()
def list_plan_operations(
    bucket: str = "",
    requires_review: str = "",
    run_name: str = "",
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """Consulta processing_plan.jsonl por bucket o revisión."""
    valid_buckets = {
        "",
        "UNIQUE",
        "DUPLICATES_EXACT",
        "REVIEW_PERCEPTUAL",
        "REVIEW_DATE",
    }
    normalized_bucket = bucket.strip().upper()
    normalized_review = requires_review.strip().lower()

    if normalized_bucket not in valid_buckets:
        raise ValueError(
            "bucket debe ser UNIQUE, DUPLICATES_EXACT, "
            "REVIEW_PERCEPTUAL, REVIEW_DATE o vacío."
        )

    if normalized_review not in {"", "true", "false"}:
        raise ValueError(
            "requires_review debe ser true, false o vacío."
        )

    review_filter = (
        None
        if normalized_review == ""
        else normalized_review == "true"
    )

    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)

    return read_plan_operations(
        run_dir=run_dir,
        bucket=normalized_bucket or None,
        requires_review=review_filter,
        offset=offset,
        limit=limit,
    )


# ---------------------------------------------------------------------
# Herramientas MCP: revisión persistente y no destructiva
# ---------------------------------------------------------------------

@mcp.tool()
def list_review_queue(
    review_type: str = "perceptual",
    decision_status: str = "pending",
    run_name: str = "",
    offset: int = 0,
    limit: int = 20,
    include_paths: bool = False,
) -> dict[str, Any]:
    """Lista grupos pendientes, resueltos o aplazados."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)

    return REVIEW_SERVICE.list_queue(
        run_dir=run_dir,
        review_type=review_type,
        decision_status=decision_status,
        offset=offset,
        limit=limit,
        include_paths=include_paths,
    )


@mcp.tool()
def get_review_progress(
    run_name: str = "",
) -> dict[str, Any]:
    """Resume el progreso de decisiones del run."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)
    return REVIEW_SERVICE.progress(run_dir)


@mcp.tool()
def get_review_decision(
    group_id: int,
    run_name: str = "",
) -> dict[str, Any]:
    """Devuelve la decisión vigente y las rutas válidas del grupo."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)
    return REVIEW_SERVICE.get_decision(run_dir, group_id)


@mcp.tool()
def save_review_decision(
    group_id: int,
    action: str,
    run_name: str = "",
    selected_path: str = "",
    note: str = "",
    reviewer: str = "user",
) -> dict[str, Any]:
    """Guarda una decisión auditable sin tocar fotos ni el plan."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)

    return REVIEW_SERVICE.save_decision(
        run_dir=run_dir,
        group_id=group_id,
        action=action,
        selected_path=selected_path,
        note=note,
        reviewer=reviewer,
    )


@mcp.tool()
def reopen_review_group(
    group_id: int,
    run_name: str = "",
    note: str = "",
    reviewer: str = "user",
) -> dict[str, Any]:
    """Reabre un grupo agregando una decisión defer al historial."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)

    return REVIEW_SERVICE.save_decision(
        run_dir=run_dir,
        group_id=group_id,
        action="defer",
        note=note or "Grupo reabierto para revisión posterior.",
        reviewer=reviewer,
    )


# ---------------------------------------------------------------------
# Herramientas MCP: clasificación y finalización no destructiva
# ---------------------------------------------------------------------

@mcp.tool()
def classify_review_groups(
    run_name: str = "",
    decision_status: str = "pending",
    offset: int = 0,
    limit: int = 50,
    include_paths: bool = False,
) -> dict[str, Any]:
    """Clasifica grupos perceptuales por metadatos sin guardar decisiones."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)

    return FINALIZATION_SERVICE.classify_groups(
        run_dir=run_dir,
        decision_status=decision_status,
        offset=offset,
        limit=limit,
        include_paths=include_paths,
    )


@mcp.tool()
def build_approved_plan(
    run_name: str = "",
    overwrite: str = "false",
) -> dict[str, Any]:
    """Genera approved_plan.jsonl; no copia, mueve ni elimina fotos."""
    normalized_overwrite = overwrite.strip().lower()

    if normalized_overwrite not in {"true", "false"}:
        raise ValueError("overwrite debe ser true o false.")

    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)

    return FINALIZATION_SERVICE.build_approved_plan(
        run_dir=run_dir,
        overwrite=normalized_overwrite == "true",
    )


@mcp.tool()
def validate_approved_plan(
    run_name: str = "",
    write_report: str = "true",
) -> dict[str, Any]:
    """Valida integridad, rutas, decisiones y ahorro del plan aprobado."""
    normalized_write = write_report.strip().lower()

    if normalized_write not in {"true", "false"}:
        raise ValueError("write_report debe ser true o false.")

    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)

    return FINALIZATION_SERVICE.validate_approved_plan(
        run_dir=run_dir,
        write_report=normalized_write == "true",
    )


@mcp.tool()
def read_run_errors(
    run_name: str = "",
    tail_lines: int = 300,
) -> dict[str, Any]:
    """Lee la parte final del log y extrae errores y advertencias."""
    config, _ = load_project_config()
    run_dir = select_run(config, run_name or None)
    log_path = run_dir / "LOGS" / "run.log"

    if not log_path.is_file():
        raise FileNotFoundError(
            f"No existe el log: {log_path}"
        )

    safe_tail = max(20, min(int(tail_lines), 2000))
    lines = log_path.read_text(
        encoding="utf-8",
        errors="replace",
    ).splitlines()
    tail = lines[-safe_tail:]

    error_lines = [
        line
        for line in tail
        if (
            " ERROR " in line
            or " - ERROR -" in line
            or "Traceback" in line
            or "FAILED" in line
        )
    ]
    warning_lines = [
        line
        for line in tail
        if (
            " WARNING " in line
            or " - WARNING -" in line
        )
    ]

    return {
        "run": run_dir.name,
        "log_file": str(log_path),
        "total_log_lines": len(lines),
        "tail_lines": tail,
        "errors": error_lines,
        "warnings": warning_lines,
    }


if __name__ == "__main__":
    try:
        mcp.run()
    except BaseException:
        error_directory = PROJECT_ROOT / ".mcp-state"
        error_directory.mkdir(parents=True, exist_ok=True)
        traceback_text = traceback.format_exc()
        (error_directory / "server-startup-error.log").write_text(
            traceback_text,
            encoding="utf-8",
        )
        print(traceback_text, file=sys.stderr, flush=True)
        raise
