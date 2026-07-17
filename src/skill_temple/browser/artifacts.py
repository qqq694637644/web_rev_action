"""Experiment manifest and local artifact persistence boundary."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .core import BrowserServiceError, Deadline, _safe_identifier, utc_now


class ExperimentStore:
    """Minimal internal persistence for sessions and experiment manifests."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.experiments_dir = self.root / "experiments"
        self.sessions_dir = self.root / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)
        self.experiments_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.recover_interrupted_experiments()

    def experiment_dir(self, experiment_id: str) -> Path:
        _safe_identifier(experiment_id, "experiment_id")
        return self.experiments_dir / experiment_id

    def _atomic_json(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def save_session(self, session: dict[str, Any]) -> None:
        session_id = _safe_identifier(str(session["session_id"]), "session_id")
        self._atomic_json(self.sessions_dir / f"{session_id}.json", session)

    def load_session(self, session_id: str) -> dict[str, Any] | None:
        path = self.sessions_dir / f"{_safe_identifier(session_id, 'session_id')}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def new_experiment_id() -> str:
        return f"exp_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:10]}"

    def create_experiment(
        self,
        *,
        session_id: str,
        operation: str,
        objective: str,
        deadline: Deadline,
        experiment_id: str | None = None,
        action_binding: dict[str, str] | None = None,
    ) -> tuple[str, Path, dict[str, Any]]:
        experiment_id = experiment_id or self.new_experiment_id()
        directory = self.experiment_dir(experiment_id)
        for child in ["playwright", "js-reverse", "reports"]:
            (directory / child).mkdir(parents=True, exist_ok=True)
        manifest = {
            "contract_version": "1.0",
            "experiment_id": experiment_id,
            "session_id": session_id,
            "operation": operation,
            "objective": objective,
            "status": "running",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "deadline": deadline.to_dict(),
            "steps": [],
            "artifacts": [],
            "warnings": [],
            "errors": [],
        }
        if action_binding:
            manifest.update(action_binding)
        self.write_manifest(experiment_id, manifest)
        return experiment_id, directory, manifest

    def write_manifest(self, experiment_id: str, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = utc_now()
        self._atomic_json(self.experiment_dir(experiment_id) / "manifest.json", manifest)

    def _load_manifest_path(self, path: Path) -> dict[str, Any]:
        relative = path.relative_to(self.root).as_posix()
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrowserServiceError(
                "manifest_invalid",
                f"Invalid experiment manifest {relative}: {type(exc).__name__}: {exc}",
                500,
            ) from exc
        if not isinstance(value, dict):
            raise BrowserServiceError(
                "manifest_invalid",
                f"Invalid experiment manifest {relative}: TypeError: expected JSON object",
                500,
            )
        return value

    def _invalid_manifest_summary(
        self,
        path: Path,
        exc: BrowserServiceError,
    ) -> dict[str, Any]:
        relative = path.relative_to(self.root).as_posix()
        cause = exc.__cause__
        error_type = type(cause).__name__ if cause is not None else "TypeError"
        return {
            "experiment_id": path.parent.name,
            "session_id": None,
            "operation": None,
            "objective": None,
            "status": "manifest_invalid",
            "created_at": None,
            "execution": None,
            "quality_summary": None,
            "manifest_relative_path": relative,
            "manifest_error": {
                "code": "manifest_invalid",
                "path": relative,
                "error_type": error_type,
                "message": str(exc),
            },
        }

    def load_manifest(self, experiment_id: str) -> dict[str, Any]:
        path = self.experiment_dir(experiment_id) / "manifest.json"
        if not path.is_file():
            raise BrowserServiceError("experiment_not_found", "Experiment was not found", 404)
        return self._load_manifest_path(path)

    def list_experiments(self, session_id: str | None, limit: int) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        manifest_errors: list[dict[str, Any]] = []
        for path in sorted(self.experiments_dir.glob("*/manifest.json"), reverse=True):
            try:
                manifest = self._load_manifest_path(path)
            except BrowserServiceError as exc:
                if len(manifest_errors) < limit:
                    manifest_errors.append(self._invalid_manifest_summary(path, exc))
                continue
            if session_id and manifest.get("session_id") != session_id:
                continue
            if len(items) >= limit:
                continue
            items.append(
                {
                    "experiment_id": manifest.get("experiment_id"),
                    "session_id": manifest.get("session_id"),
                    "operation": manifest.get("operation"),
                    "objective": manifest.get("objective"),
                    "status": manifest.get("status"),
                    "created_at": manifest.get("created_at"),
                    "execution": manifest.get("execution"),
                    "quality_summary": manifest.get("quality_summary"),
                }
            )
        return {
            "experiments": items,
            "manifest_errors": manifest_errors,
        }

    def recover_interrupted_experiments(self) -> int:
        recovered = 0
        for path in self.experiments_dir.glob("*/manifest.json"):
            try:
                manifest = self._load_manifest_path(path)
            except BrowserServiceError:
                continue
            if manifest.get("status") != "running":
                continue
            manifest["status"] = "interrupted"
            manifest["interrupted_at"] = utc_now()
            manifest["errors"] = [
                *(manifest.get("errors") if isinstance(manifest.get("errors"), list) else []),
                "The service restarted before this experiment reached a terminal manifest.",
            ]
            self.write_manifest(str(manifest["experiment_id"]), manifest)
            recovered += 1
        return recovered

    def describe_local_artifact(
        self,
        path_value: str,
        *,
        artifact_id: str,
        kind: str,
        sensitivity: str = "private",
        contains_credentials: bool = False,
    ) -> dict[str, Any] | None:
        path = Path(path_value)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return None
        if not path.is_file():
            return None
        return {
            "artifactId": artifact_id,
            "kind": kind,
            "relativePath": relative.as_posix(),
            "bytes": path.stat().st_size,
            "sensitivity": sensitivity,
            "containsCredentials": contains_credentials,
        }

    def relative_path(self, path_value: str) -> str | None:
        path = Path(path_value)
        if not path.is_absolute():
            path = self.root / path
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None
