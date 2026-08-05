from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


@dataclass
class JobRecord:
    job_id: str
    pid: int
    status: str
    started_at: str
    config_path: str
    output_directory: str
    stdout_path: str
    stderr_path: str
    before_runs: list[str]
    ended_at: str | None = None
    return_code: int | None = None
    run_name: str | None = None


class JobManager:
    """Administra dry-runs persistentes sin bloquear una llamada MCP."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.state_directory = self.project_root / ".mcp-state"
        self.jobs_directory = self.state_directory / "jobs"
        self.lock_path = self.state_directory / "dry-run.lock.json"

        self.jobs_directory.mkdir(parents=True, exist_ok=True)

        self._processes: dict[str, subprocess.Popen[Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False

        if os.name == "nt":
            result = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    f"PID eq {pid}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                encoding="utf-8",
                errors="replace",
            )
            return result.returncode == 0 and f'"{pid}"' in result.stdout

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

        return True

    def _job_directory(self, job_id: str) -> Path:
        normalized = job_id.replace("_", "").replace("-", "")

        if not normalized.isalnum():
            raise ValueError("job_id inválido.")

        return self.jobs_directory / job_id

    def _state_path(self, job_id: str) -> Path:
        return self._job_directory(job_id) / "state.json"

    def _write_record(self, record: JobRecord) -> None:
        path = self._state_path(record.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)

        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                asdict(record),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _read_record(self, job_id: str) -> JobRecord:
        path = self._state_path(job_id)

        if not path.is_file():
            raise FileNotFoundError(f"No existe el trabajo: {job_id}")

        return JobRecord(
            **json.loads(path.read_text(encoding="utf-8"))
        )

    def _read_lock(self) -> dict[str, Any] | None:
        if not self.lock_path.is_file():
            return None

        try:
            value = json.loads(
                self.lock_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            self.lock_path.unlink(missing_ok=True)
            return None

        if not isinstance(value, dict) or not value.get("job_id"):
            self.lock_path.unlink(missing_ok=True)
            return None

        return value

    def _clear_lock(self, job_id: str) -> None:
        lock = self._read_lock()

        if lock and lock.get("job_id") == job_id:
            self.lock_path.unlink(missing_ok=True)

    def _find_new_run(self, record: JobRecord) -> str | None:
        output = Path(record.output_directory)

        if not output.is_dir():
            return record.run_name

        previous = set(record.before_runs)
        candidates = sorted(
            path.name
            for path in output.iterdir()
            if (
                path.is_dir()
                and path.name.startswith("run_")
                and path.name not in previous
            )
        )

        return candidates[-1] if candidates else record.run_name

    @staticmethod
    def _run_looks_completed(record: JobRecord) -> bool:
        if not record.run_name:
            return False

        run_directory = (
            Path(record.output_directory) / record.run_name
        )
        summary_path = (
            run_directory / "REPORTS" / "run_summary.txt"
        )
        state_path = (
            run_directory / "MANIFESTS" / "run_state.json"
        )

        if summary_path.is_file():
            return True

        if not state_path.is_file():
            return False

        try:
            state = json.loads(
                state_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            return False

        status = str(
            state.get("status")
            or state.get("state")
            or ""
        ).lower()

        return status in {
            "planned",
            "completed",
            "complete",
            "success",
            "succeeded",
        }

    def start_dry_run(
        self,
        config_path: Path,
        output_directory: Path,
    ) -> dict[str, Any]:
        with self._lock:
            active = self.get_active_job()

            if active is not None:
                return {
                    "started": False,
                    "reason": "Ya existe un dry-run activo.",
                    "active_job": active,
                }

            before_runs = (
                sorted(
                    path.name
                    for path in output_directory.iterdir()
                    if (
                        path.is_dir()
                        and path.name.startswith("run_")
                    )
                )
                if output_directory.is_dir()
                else []
            )

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            job_id = (
                f"dryrun_{timestamp}_{uuid.uuid4().hex[:6]}"
            )

            job_directory = self._job_directory(job_id)
            job_directory.mkdir(parents=True, exist_ok=True)

            stdout_path = job_directory / "stdout.log"
            stderr_path = job_directory / "stderr.log"

            command = [
                sys.executable,
                "-u",
                "-m",
                "photos_dedupe",
                "--config",
                str(config_path),
                "--action",
                "dry-run",
            ]

            popen_options: dict[str, Any] = {}

            if os.name == "nt":
                popen_options["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                )
            else:
                popen_options["start_new_session"] = True

            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"

            with (
                stdout_path.open("ab", buffering=0) as stdout_file,
                stderr_path.open("ab", buffering=0) as stderr_file,
            ):
                process = subprocess.Popen(
                    command,
                    cwd=self.project_root,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    shell=False,
                    env=environment,
                    **popen_options,
                )

            record = JobRecord(
                job_id=job_id,
                pid=process.pid,
                status="running",
                started_at=self._now(),
                config_path=str(config_path),
                output_directory=str(output_directory),
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
                before_runs=before_runs,
            )

            self._processes[job_id] = process
            self._write_record(record)

            self.lock_path.write_text(
                json.dumps(
                    {
                        "job_id": job_id,
                        "pid": process.pid,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            return {
                "started": True,
                "job_id": job_id,
                "pid": process.pid,
                "status": "running",
                "poll_after_seconds": 20,
            }

    def get_status(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._read_record(job_id)
            record.run_name = self._find_new_run(record)

            process = self._processes.get(job_id)

            if process is not None:
                return_code = process.poll()

                if return_code is None:
                    record.status = "running"
                else:
                    record.return_code = return_code
                    record.ended_at = (
                        record.ended_at or self._now()
                    )
                    record.status = (
                        "completed"
                        if return_code == 0
                        else "failed"
                    )
                    self._processes.pop(job_id, None)
                    self._clear_lock(job_id)

            elif record.status not in FINAL_STATUSES:
                if self._pid_exists(record.pid):
                    record.status = "running"
                else:
                    record.ended_at = (
                        record.ended_at or self._now()
                    )

                    if self._run_looks_completed(record):
                        record.status = "completed"
                        record.return_code = (
                            0
                            if record.return_code is None
                            else record.return_code
                        )
                    else:
                        record.status = "interrupted"

                    self._clear_lock(job_id)

            self._write_record(record)
            return asdict(record)

    def get_active_job(self) -> dict[str, Any] | None:
        lock = self._read_lock()

        if lock is None:
            return None

        try:
            status = self.get_status(str(lock["job_id"]))
        except (FileNotFoundError, ValueError):
            self.lock_path.unlink(missing_ok=True)
            return None

        if status["status"] == "running":
            return status

        self._clear_lock(str(lock["job_id"]))
        return None

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._read_record(job_id)
            current = self.get_status(job_id)

            if current["status"] != "running":
                return {
                    "cancelled": False,
                    "reason": "El proceso no está activo.",
                    "job": current,
                }

            if os.name == "nt":
                result = subprocess.run(
                    [
                        "taskkill",
                        "/PID",
                        str(record.pid),
                        "/T",
                        "/F",
                    ],
                    shell=False,
                    capture_output=True,
                    text=True,
                    check=False,
                    encoding="utf-8",
                    errors="replace",
                )
                cancellation_details = {
                    "return_code": result.returncode,
                    "stdout": result.stdout[-2000:],
                    "stderr": result.stderr[-2000:],
                }
            else:
                try:
                    os.killpg(
                        os.getpgid(record.pid),
                        signal.SIGTERM,
                    )
                    cancellation_details = {
                        "return_code": 0,
                    }
                except ProcessLookupError:
                    cancellation_details = {
                        "return_code": 1,
                        "stderr": "El proceso ya no existe.",
                    }

            record.status = "cancelled"
            record.ended_at = self._now()
            process = self._processes.pop(job_id, None)

            if process is not None:
                record.return_code = process.poll()

            self._clear_lock(job_id)
            self._write_record(record)

            return {
                "cancelled": True,
                "job": asdict(record),
                "details": cancellation_details,
            }

    def read_logs(
        self,
        job_id: str,
        lines: int = 80,
    ) -> dict[str, Any]:
        record = self._read_record(job_id)
        safe_lines = max(10, min(int(lines), 300))

        def tail(path_value: str) -> list[str]:
            path = Path(path_value)

            if not path.is_file():
                return []

            return path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()[-safe_lines:]

        return {
            "job_id": job_id,
            "stdout": tail(record.stdout_path),
            "stderr": tail(record.stderr_path),
        }

    def list_jobs(self, limit: int = 20) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 100))
        records: list[dict[str, Any]] = []

        state_paths = sorted(
            self.jobs_directory.glob("*/state.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

        for path in state_paths[:safe_limit]:
            try:
                record = JobRecord(
                    **json.loads(path.read_text(encoding="utf-8"))
                )
                records.append(self.get_status(record.job_id))
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue

        return {
            "total_found": len(state_paths),
            "returned": len(records),
            "jobs": records,
        }
