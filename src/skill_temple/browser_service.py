"""Atomic browser experiment orchestration and workspace evidence storage."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .browser_adapters import (
    AlignmentResult,
    JsReverseAdapter,
    JsReverseMcpAdapter,
    McpToolTransport,
    PlaywrightAdapter,
    PlaywrightCliAdapter,
    StdioMcpToolTransport,
)
from .browser_models import (
    BrowserActionResponse,
    CaptureBaselineRequest,
    CaptureFlowPayload,
    CaptureFlowRequest,
    CloseSessionRequest,
    ExportExperimentRequest,
    FlowStep,
    FlowStepResult,
    GetExperimentRequest,
    GetSessionRequest,
    GetStreamStatusRequest,
    InspectBrowserEvidenceRequest,
    ListArtifactsRequest,
    ListExperimentsRequest,
    OpenSessionRequest,
    ReadArtifactRequest,
    RequestMatcher,
    RunBrowserExperimentRequest,
    SearchArtifactsRequest,
    WaitCondition,
)
from .runtime import env_value_from_environment_or_dotenv


class BrowserServiceError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class Deadline:
    def __init__(self, timeout_ms: int) -> None:
        self.started_monotonic = time.monotonic()
        self.started_wall_time_ms = int(time.time() * 1000)
        self.deadline_monotonic = self.started_monotonic + timeout_ms / 1000
        self.deadline_wall_time_ms = self.started_wall_time_ms + timeout_ms
        self.timeout_ms = timeout_ms

    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline_monotonic - time.monotonic())

    def remaining_ms(self) -> int:
        return max(0, int(self.remaining_seconds() * 1000))

    def ensure_remaining(self, operation: str) -> None:
        if self.remaining_seconds() <= 0:
            raise BrowserServiceError(
                "deadline_exceeded",
                f"Deadline exceeded before {operation}",
                504,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_ms": self.timeout_ms,
            "started_wall_time_ms": self.started_wall_time_ms,
            "deadline_wall_time_ms": self.deadline_wall_time_ms,
            "remaining_ms": self.remaining_ms(),
        }

    def child(self, timeout_ms: int) -> Deadline:
        child = object.__new__(Deadline)
        child.started_monotonic = time.monotonic()
        child.started_wall_time_ms = int(time.time() * 1000)
        requested_seconds = max(0.001, timeout_ms / 1000)
        child.deadline_monotonic = min(
            self.deadline_monotonic,
            child.started_monotonic + requested_seconds,
        )
        child.deadline_wall_time_ms = min(
            self.deadline_wall_time_ms,
            child.started_wall_time_ms + timeout_ms,
        )
        child.timeout_ms = min(timeout_ms, self.remaining_ms())
        return child


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_identifier(value: str, label: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", value):
        raise BrowserServiceError("invalid_identifier", f"Invalid {label}: {value}")
    return value


class LocalEvidenceStore:
    """Owns Action-local evidence. It is not a Gateway or Git workspace."""

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

    def create_experiment(
        self,
        *,
        session_id: str,
        operation: str,
        objective: str,
        deadline: Deadline,
    ) -> tuple[str, Path, dict[str, Any]]:
        experiment_id = f"exp_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:10]}"
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
        self.write_manifest(experiment_id, manifest)
        return experiment_id, directory, manifest

    def write_manifest(self, experiment_id: str, manifest: dict[str, Any]) -> None:
        manifest["updated_at"] = utc_now()
        self._atomic_json(self.experiment_dir(experiment_id) / "manifest.json", manifest)

    def load_manifest(self, experiment_id: str) -> dict[str, Any]:
        path = self.experiment_dir(experiment_id) / "manifest.json"
        if not path.is_file():
            raise BrowserServiceError("experiment_not_found", "Experiment was not found", 404)
        return json.loads(path.read_text(encoding="utf-8"))

    def list_experiments(self, session_id: str | None, limit: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.experiments_dir.glob("*/manifest.json"), reverse=True):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if session_id and manifest.get("session_id") != session_id:
                continue
            items.append(
                {
                    "experiment_id": manifest.get("experiment_id"),
                    "session_id": manifest.get("session_id"),
                    "operation": manifest.get("operation"),
                    "objective": manifest.get("objective"),
                    "status": manifest.get("status"),
                    "created_at": manifest.get("created_at"),
                    "objective_integrity": manifest.get("objective_integrity"),
                }
            )
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _artifact_id(descriptor: dict[str, Any]) -> str | None:
        value = descriptor.get("artifactId") or descriptor.get("artifact_id")
        return str(value) if value else None

    def list_artifacts(
        self, experiment_id: str, offset: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        manifest = self.load_manifest(experiment_id)
        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, list):
            return []
        items = [item for item in artifacts if isinstance(item, dict)]
        return items[offset:] if limit is None else items[offset : offset + limit]

    def recover_interrupted_experiments(self) -> int:
        recovered = 0
        for path in self.experiments_dir.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("status") != "running":
                continue
            manifest["status"] = "interrupted"
            manifest["interrupted_at"] = utc_now()
            manifest["errors"] = [
                *(
                    manifest.get("errors")
                    if isinstance(manifest.get("errors"), list)
                    else []
                ),
                "The service restarted before this experiment reached a terminal manifest.",
            ]
            self.write_manifest(str(manifest["experiment_id"]), manifest)
            recovered += 1
        return recovered

    def _resolve_relative(self, relative_path: str) -> Path:
        path = (self.root / Path(relative_path)).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise BrowserServiceError(
                "unsafe_artifact_path",
                "Artifact path escaped workspace",
                400,
            ) from exc
        return path

    def read_artifact(
        self,
        *,
        experiment_id: str,
        artifact_id: str,
        offset: int,
        max_bytes: int,
        credential_mode: str,
    ) -> dict[str, Any]:
        artifacts = self.list_artifacts(experiment_id)
        descriptor = next(
            (item for item in artifacts if self._artifact_id(item) == artifact_id),
            None,
        )
        if descriptor is None:
            raise BrowserServiceError("artifact_not_found", "Artifact was not found", 404)
        if descriptor.get("sensitivity") == "credential" and credential_mode != "full":
            redacted_id = descriptor.get("redactedArtifactId") or descriptor.get(
                "redacted_artifact_id"
            )
            if redacted_id:
                return self.read_artifact(
                    experiment_id=experiment_id,
                    artifact_id=str(redacted_id),
                    offset=offset,
                    max_bytes=max_bytes,
                    credential_mode="redacted",
                )
            return {
                "artifact": descriptor,
                "credential_redacted": True,
                "content": None,
                "truncated": False,
                "next_offset": None,
            }
        relative_path = descriptor.get("relativePath") or descriptor.get("relative_path")
        if not isinstance(relative_path, str):
            raise BrowserServiceError("artifact_path_missing", "Artifact has no relative path", 409)
        path = self._resolve_relative(relative_path)
        if not path.is_file():
            raise BrowserServiceError("artifact_file_missing", "Artifact file is unavailable", 404)
        with path.open("rb") as handle:
            handle.seek(offset)
            payload = handle.read(max_bytes + 1)
        truncated = len(payload) > max_bytes
        payload = payload[:max_bytes]
        return {
            "artifact": descriptor,
            "offset": offset,
            "bytes_returned": len(payload),
            "content": payload.decode("utf-8", errors="replace"),
            "truncated": truncated,
            "next_offset": offset + len(payload) if truncated else None,
            "credential_redacted": descriptor.get("sensitivity") != "credential",
        }

    def _effective_descriptor(
        self,
        experiment_id: str,
        descriptor: dict[str, Any],
        credential_mode: str,
    ) -> dict[str, Any] | None:
        if descriptor.get("sensitivity") != "credential" or credential_mode == "full":
            return descriptor
        redacted_id = descriptor.get("redactedArtifactId") or descriptor.get(
            "redacted_artifact_id"
        )
        if not redacted_id:
            return None
        return next(
            (
                item
                for item in self.list_artifacts(experiment_id)
                if self._artifact_id(item) == str(redacted_id)
            ),
            None,
        )

    def search_artifacts(
        self,
        *,
        experiment_id: str,
        query: str,
        artifact_kinds: list[str],
        max_matches: int,
        max_bytes_per_artifact: int,
        credential_mode: str,
    ) -> list[dict[str, Any]]:
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        for original in self.list_artifacts(experiment_id):
            descriptor = self._effective_descriptor(
                experiment_id, original, credential_mode
            )
            if descriptor is None:
                continue
            artifact_id = self._artifact_id(descriptor)
            if not artifact_id or artifact_id in seen:
                continue
            seen.add(artifact_id)
            if artifact_kinds and str(descriptor.get("kind")) not in artifact_kinds:
                continue
            relative_path = descriptor.get("relativePath") or descriptor.get(
                "relative_path"
            )
            if not isinstance(relative_path, str):
                continue
            path = self._resolve_relative(relative_path)
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                payload = handle.read(max_bytes_per_artifact)
            text = payload.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), start=1):
                position = line.casefold().find(needle)
                if position < 0:
                    continue
                start = max(0, position - 120)
                end = min(len(line), position + len(query) + 120)
                matches.append(
                    {
                        "artifact_id": artifact_id,
                        "kind": descriptor.get("kind"),
                        "relative_path": relative_path,
                        "line_number": line_number,
                        "snippet": line[start:end],
                        "credential_redacted": (
                            original.get("sensitivity") == "credential"
                            and credential_mode != "full"
                        ),
                    }
                )
                if len(matches) >= max_matches:
                    return matches
        return matches

    def export_experiment(
        self, experiment_id: str, *, include_credentials: bool
    ) -> dict[str, Any]:
        directory = self.experiment_dir(experiment_id)
        manifest = self.load_manifest(experiment_id)
        exports = directory / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        archive = exports / f"{experiment_id}.zip"
        credential_paths = {
            str(item.get("relativePath") or item.get("relative_path"))
            for item in self.list_artifacts(experiment_id)
            if item.get("sensitivity") == "credential"
        }
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as bundle:
            for path in directory.rglob("*"):
                if not path.is_file() or exports in path.parents:
                    continue
                relative_to_root = path.relative_to(self.root).as_posix()
                if not include_credentials and relative_to_root in credential_paths:
                    continue
                bundle.write(path, path.relative_to(directory).as_posix())
        descriptor = self.describe_local_artifact(
            archive.as_posix(),
            artifact_id=f"art_{experiment_id}_export",
            kind="experiment_export",
            sensitivity="credential" if include_credentials else "private",
        )
        if descriptor is None:
            raise BrowserServiceError("export_failed", "Experiment export was not created", 500)
        artifacts = [
            item
            for item in manifest.get("artifacts", [])
            if isinstance(item, dict)
            and self._artifact_id(item) != descriptor["artifactId"]
        ]
        artifacts.append(descriptor)
        manifest["artifacts"] = artifacts
        self.write_manifest(experiment_id, manifest)
        return descriptor

    def describe_local_artifact(
        self,
        path_value: str,
        *,
        artifact_id: str,
        kind: str,
        sensitivity: str = "private",
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
            "containsCredentials": False,
        }

    def relative_path(self, path_value: str) -> str | None:
        path = Path(path_value)
        if not path.is_absolute():
            path = self.root / path
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None


class BrowserActionService:
    FINALIZE_RESERVE_MS = 2_000
    STREAM_WAIT_TYPES = {
        "request_observed",
        "response_observed",
        "first_event",
        "event_predicate",
        "default_done_marker",
        "network_finished",
        "network_canceled",
        "failed",
    }

    def __init__(
        self,
        *,
        playwright: PlaywrightAdapter,
        js_reverse: JsReverseAdapter,
        evidence: LocalEvidenceStore,
        default_browser_endpoint: str | None = None,
        private_mcp_browser_endpoint: str | None = None,
        require_private_mcp_endpoint: bool = False,
    ) -> None:
        self.playwright = playwright
        self.js_reverse = js_reverse
        self.evidence = evidence
        self.default_browser_endpoint = default_browser_endpoint
        self.private_mcp_browser_endpoint = private_mcp_browser_endpoint
        self.require_private_mcp_endpoint = require_private_mcp_endpoint
        self.sessions: dict[str, dict[str, Any]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._jobs: dict[str, asyncio.Task[None]] = {}

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def _get_session(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id) or self.evidence.load_session(session_id)
        if not session:
            raise BrowserServiceError("session_not_found", "Browser session was not found", 404)
        self.sessions[session_id] = session
        return session

    async def run(self, request: RunBrowserExperimentRequest) -> BrowserActionResponse:
        if isinstance(request, ExportExperimentRequest):
            descriptor = self.evidence.export_experiment(
                request.payload.experiment_id,
                include_credentials=request.payload.include_credentials,
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                experiment_id=request.payload.experiment_id,
                result={"export_artifact": descriptor},
            )
        if isinstance(request, OpenSessionRequest):
            return await self._open_session(request)
        if isinstance(request, CloseSessionRequest):
            return await self._close_session(request)
        if isinstance(request, (CaptureFlowRequest, CaptureBaselineRequest)):
            if request.payload.execution_mode == "job":
                return self._start_capture_job(request)
            return await self._capture_flow(request)
        raise BrowserServiceError("unsupported_operation", "Unsupported browser operation", 400)

    async def inspect(self, request: InspectBrowserEvidenceRequest) -> BrowserActionResponse:
        if isinstance(request, GetSessionRequest):
            session = self._get_session(request.payload.session_id)
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=request.payload.session_id,
                result={"session": session},
            )
        if isinstance(request, ListExperimentsRequest):
            items = self.evidence.list_experiments(
                request.payload.session_id, request.payload.limit
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                result={"experiments": items, "count": len(items)},
            )
        if isinstance(request, GetExperimentRequest):
            manifest = self.evidence.load_manifest(request.payload.experiment_id)
            manifest_status = str(manifest.get("status", "partial"))
            response_status = (
                manifest_status
                if manifest_status
                in {"running", "completed", "failed", "partial", "interrupted"}
                else "partial"
            )
            return BrowserActionResponse(
                operation=request.operation,
                status=response_status,
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"experiment": manifest},
            )
        if isinstance(request, ListArtifactsRequest):
            artifacts = self.evidence.list_artifacts(
                request.payload.experiment_id,
                request.payload.offset,
                request.payload.limit,
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                experiment_id=request.payload.experiment_id,
                result={"artifacts": artifacts, "count": len(artifacts)},
            )
        if isinstance(request, ReadArtifactRequest):
            result = self.evidence.read_artifact(
                experiment_id=request.payload.experiment_id,
                artifact_id=request.payload.artifact_id,
                offset=request.payload.offset,
                max_bytes=request.payload.max_bytes,
                credential_mode=request.payload.credential_mode,
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                experiment_id=request.payload.experiment_id,
                result=result,
            )
        if isinstance(request, SearchArtifactsRequest):
            matches = self.evidence.search_artifacts(
                experiment_id=request.payload.experiment_id,
                query=request.payload.query,
                artifact_kinds=request.payload.artifact_kinds,
                max_matches=request.payload.max_matches,
                max_bytes_per_artifact=request.payload.max_bytes_per_artifact,
                credential_mode=request.payload.credential_mode,
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                experiment_id=request.payload.experiment_id,
                result={"matches": matches, "count": len(matches)},
            )
        if isinstance(request, GetStreamStatusRequest):
            self._get_session(request.payload.session_id)
            deadline = Deadline(10_000)
            status = await self.js_reverse.get_stream_status(request.payload.capture_id, deadline)
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=request.payload.session_id,
                result={"stream": status},
            )
        raise BrowserServiceError("unsupported_operation", "Unsupported inspect operation", 400)

    async def _open_session(self, request: OpenSessionRequest) -> BrowserActionResponse:
        payload = request.payload
        deadline = Deadline(payload.deadline_ms)
        session_id = payload.session_id or f"sess_{uuid.uuid4().hex[:12]}"
        _safe_identifier(session_id, "session_id")
        endpoint = payload.browser_endpoint or self.default_browser_endpoint
        if not endpoint:
            raise BrowserServiceError(
                "browser_endpoint_missing",
                "Provide browser_endpoint or configure WEB_REV_BROWSER_CDP_URL",
                503,
            )
        if self.require_private_mcp_endpoint and not self.private_mcp_browser_endpoint:
            raise BrowserServiceError(
                "private_mcp_endpoint_missing",
                "Configure WEB_REV_BROWSER_CDP_URL before running browser experiments",
                503,
            )
        if (
            self.private_mcp_browser_endpoint
            and endpoint != self.private_mcp_browser_endpoint
        ):
            raise BrowserServiceError(
                "browser_endpoint_mismatch",
                "Playwright and js-reverse-mcp must use the same CDP endpoint",
                409,
            )
        async with self._session_lock(session_id):
            page = await self.playwright.open_session(
                session_id, endpoint, payload.target.start_url, deadline
            )
            if payload.target.page_index != page.page_index:
                page = await self.playwright.select_page(
                    session_id,
                    payload.target.page_index,
                    deadline,
                )
            alignment = await self.js_reverse.align_page(page, deadline)
            if alignment.status != "aligned":
                await self.playwright.close_session(session_id, deadline)
                raise BrowserServiceError(
                    "page_alignment_failed",
                    "; ".join(alignment.warnings) or "Could not align browser page",
                    409,
                )
            now = utc_now()
            session = {
                "session_id": session_id,
                "status": "open",
                "browser_endpoint_ref": endpoint,
                "playwright_session_ref": session_id,
                "playwright_page_index": page.page_index,
                "playwright_page_url": page.url,
                "playwright_page_title": page.title,
                "js_reverse_page_index": alignment.js_reverse_page_index,
                "js_reverse_page_id": alignment.js_reverse_page_id,
                "js_reverse_page_url": alignment.js_reverse_page_url,
                "page_alignment_status": alignment.status,
                "evidence_store": "local",
                "evidence_root_ref": ".",
                "created_at": now,
                "updated_at": now,
            }
            self.sessions[session_id] = session
            self.evidence.save_session(session)
        return BrowserActionResponse(
            operation=request.operation,
            status="completed",
            session_id=session_id,
            result={"session": session, "alignment": asdict(alignment)},
            warnings=alignment.warnings,
        )

    async def _close_session(self, request: CloseSessionRequest) -> BrowserActionResponse:
        deadline = Deadline(request.payload.deadline_ms)
        session_id = request.payload.session_id
        async with self._session_lock(session_id):
            session = self._get_session(session_id)
            await self.playwright.close_session(session_id, deadline)
            session["status"] = "closed"
            session["updated_at"] = utc_now()
            self.evidence.save_session(session)
        return BrowserActionResponse(
            operation=request.operation,
            status="completed",
            session_id=session_id,
            result={"session": session},
        )

    async def _align_session(
        self, session: dict[str, Any], payload: CaptureFlowPayload, deadline: Deadline
    ) -> AlignmentResult:
        page = await self.playwright.select_page(
            str(session["playwright_session_ref"]),
            payload.target.page_index,
            deadline,
        )
        if payload.target.start_url:
            step = FlowStep(
                step_id="__target_navigation__",
                action="navigate",
                value=payload.target.start_url,
                timeout_ms=min(5_000, deadline.remaining_ms()),
            )
            await self.playwright.execute_step(
                str(session["playwright_session_ref"]),
                step,
                self.evidence.root,
                deadline,
            )
        page = await self.playwright.current_page(
            str(session["playwright_session_ref"]), deadline
        )
        if (
            payload.target.expected_url_contains
            and payload.target.expected_url_contains not in page.url
        ):
            raise BrowserServiceError(
                "unexpected_page_url",
                f"Current page URL does not contain {payload.target.expected_url_contains}",
                409,
            )
        alignment = await self.js_reverse.align_page(
            page,
            deadline,
            page_id=(
                str(session["js_reverse_page_id"])
                if session.get("js_reverse_page_id")
                else None
            ),
        )
        if alignment.status != "aligned":
            raise BrowserServiceError(
                "page_alignment_failed",
                "; ".join(alignment.warnings) or "Could not align the current page",
                409,
            )
        session.update(
            {
                "playwright_page_url": page.url,
                "playwright_page_title": page.title,
                "playwright_page_index": page.page_index,
                "js_reverse_page_index": alignment.js_reverse_page_index,
                "js_reverse_page_id": alignment.js_reverse_page_id,
                "js_reverse_page_url": alignment.js_reverse_page_url,
                "page_alignment_status": alignment.status,
                "updated_at": utc_now(),
            }
        )
        self.evidence.save_session(session)
        return alignment

    @staticmethod
    def _request_matcher(payload: CaptureFlowPayload) -> RequestMatcher:
        return RequestMatcher(
            url_contains=payload.primary_request.url_contains,
            method=payload.primary_request.method,
            resource_types=payload.primary_request.resource_types,
            mime_types=payload.primary_request.mime_types,
        )

    async def _wait_condition(
        self,
        *,
        session_ref: str,
        capture_id: int | None,
        condition: WaitCondition,
        since_version: int,
        deadline: Deadline,
    ) -> dict[str, Any]:
        condition_deadline = deadline.child(condition.timeout_ms)
        if (
            condition.type == "event_predicate"
            and condition.predicate
            and condition.predicate.type == "selector_state"
        ):
            page_condition = WaitCondition(
                type=(
                    "selector_visible"
                    if condition.predicate.value == "visible"
                    else "selector_hidden"
                ),
                timeout_ms=condition.timeout_ms,
                locator=condition.predicate.locator,
            )
            return await self.playwright.wait_for_page_condition(
                session_ref,
                page_condition,
                condition_deadline,
            )
        if condition.type in self.STREAM_WAIT_TYPES:
            if capture_id is None:
                raise BrowserServiceError(
                    "stream_capture_required",
                    f"Wait condition {condition.type} requires stream capture",
                    409,
                )
            result = await self.js_reverse.wait_for_stream_condition(
                capture_id=capture_id,
                request_matcher=condition.request_matcher or RequestMatcher(),
                condition=condition,
                since_version=since_version,
                deadline=condition_deadline,
            )
            return asdict(result)
        return await self.playwright.wait_for_page_condition(
            session_ref,
            condition,
            condition_deadline,
        )

    def _ensure_finalize_reserve(self, deadline: Deadline, operation: str) -> None:
        if deadline.remaining_ms() <= self.FINALIZE_RESERVE_MS:
            raise BrowserServiceError(
                "deadline_finalize_reserve",
                f"Stopped before {operation} to preserve stream finalization time",
                504,
            )

    def _operation_deadline(
        self,
        deadline: Deadline,
        requested_ms: int,
        operation: str,
    ) -> Deadline:
        self._ensure_finalize_reserve(deadline, operation)
        available_ms = deadline.remaining_ms() - self.FINALIZE_RESERVE_MS
        if available_ms <= 0:
            raise BrowserServiceError(
                "deadline_finalize_reserve",
                f"No execution budget remains for {operation}",
                504,
            )
        return deadline.child(min(requested_ms, available_ms))

    @staticmethod
    def _integrity_severity(value: str) -> int:
        return {"complete": 0, "semantic-only": 1, "partial": 2, "failed": 3}.get(value, 2)

    def _primary_result(
        self,
        payload: CaptureFlowPayload,
        status_payload: dict[str, Any],
        network_payload: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str, bool]:
        matcher = self._request_matcher(payload)
        requests = [
            item
            for item in status_payload.get("requests", [])
            if isinstance(item, dict)
            and JsReverseMcpAdapter._request_matches(item, matcher)
        ]
        if not requests:
            requests = [
                {
                    **item,
                    "integrityStatus": item.get("integrityStatus", "partial"),
                    "evidenceSource": "network-summary",
                }
                for item in network_payload.get("requests", [])
                if isinstance(item, dict)
                and JsReverseMcpAdapter._request_matches(item, matcher)
            ]
        count_ok = (
            payload.primary_request.expected_min_matches
            <= len(requests)
            <= payload.primary_request.expected_max_matches
        )
        if not requests:
            integrity = (
                "failed" if payload.primary_request.expected_min_matches else "partial"
            )
            return requests, integrity, count_ok
        integrity = max(
            (str(item.get("integrityStatus", "partial")) for item in requests),
            key=self._integrity_severity,
        )
        return requests, integrity, count_ok

    @staticmethod
    def _classify_cancellations(
        payload: CaptureFlowPayload,
        step_results: list[FlowStepResult],
        primary_requests: list[dict[str, Any]],
        alignment: AlignmentResult,
        wait_observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        completed_by_id = {
            result.step_id: result
            for result in step_results
            if result.status == "completed"
        }
        classifications: list[dict[str, Any]] = []
        for index, step in enumerate(payload.flow):
            if step.intent != "stop_generation" or step.step_id not in completed_by_id:
                continue
            later_navigation = any(
                later.action in {"navigate", "reload"}
                for later in payload.flow[index + 1 :]
            )
            result = completed_by_id[step.step_id]
            before_observation = next(
                (
                    item
                    for item in reversed(wait_observations)
                    if int(item.get("step_index", -1)) < index
                    and item.get("condition_type")
                    in {"first_event", "event_predicate"}
                ),
                None,
            )
            after_observation = next(
                (
                    item
                    for item in wait_observations
                    if int(item.get("step_index", -1)) > index
                    and item.get("condition_type") == "network_canceled"
                ),
                None,
            )
            try:
                stop_wall_ms = int(
                    datetime.fromisoformat(result.ended_at).timestamp() * 1000
                )
            except ValueError:
                continue
            for request in primary_requests:
                if request.get("status") != "canceled":
                    continue
                if request.get("terminalReason") != "network_canceled":
                    continue
                ended_wall_ms = request.get("endedWallTimeMs")
                within_window = isinstance(ended_wall_ms, (int, float)) and (
                    -500 <= ended_wall_ms - stop_wall_ms <= 5_000
                )
                expected = (
                    alignment.status == "aligned"
                    and within_window
                    and not later_navigation
                )
                classification = {
                    "request_id": request.get("cdpRequestId"),
                    "persistent_request_id": request.get("persistentRequestId"),
                    "source_terminal_reason": "network_canceled",
                    "classification": (
                        "expected_user_cancel"
                        if expected
                        else "unclassified_network_cancel"
                    ),
                    "stop_step_id": step.step_id,
                    "within_stop_window": within_window,
                    "page_aligned": alignment.status == "aligned",
                    "later_navigation": later_navigation,
                    "stream_before_stop": (
                        before_observation.get("matched_event")
                        if before_observation
                        else None
                    ),
                    "stream_after_stop": (
                        after_observation.get("matched_event")
                        if after_observation
                        else None
                    ),
                }
                request["experimentCancellationClassification"] = classification[
                    "classification"
                ]
                classifications.append(classification)
        return classifications

    @staticmethod
    def _collect_artifacts(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
        artifacts: dict[str, dict[str, Any]] = {}

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                artifact_id = value.get("artifactId") or value.get("artifact_id")
                relative_path = value.get("relativePath") or value.get("relative_path")
                if artifact_id and relative_path:
                    artifacts[str(artifact_id)] = value
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for payload in payloads:
            visit(payload)
        return list(artifacts.values())

    def _start_capture_job(
        self, request: CaptureFlowRequest | CaptureBaselineRequest
    ) -> BrowserActionResponse:
        payload = request.payload
        session = self._get_session(payload.session_id)
        if session.get("status") != "open":
            raise BrowserServiceError("session_closed", "Browser session is not open", 409)
        deadline = Deadline(payload.job_timeout_ms)
        experiment_id, experiment_dir, manifest = self.evidence.create_experiment(
            session_id=payload.session_id,
            operation=request.operation,
            objective=payload.objective,
            deadline=deadline,
        )
        manifest.update(
            {
                "execution_mode": "job",
                "job_timeout_ms": payload.job_timeout_ms,
                "primary_request_matcher": payload.primary_request.model_dump(
                    mode="json", exclude_none=True
                ),
            }
        )
        self.evidence.write_manifest(experiment_id, manifest)
        task = asyncio.create_task(
            self._run_capture_job(
                request,
                deadline=deadline,
                prepared=(experiment_id, experiment_dir, manifest),
            ),
            name=f"browser-experiment-{experiment_id}",
        )
        self._jobs[experiment_id] = task
        task.add_done_callback(lambda _task: self._jobs.pop(experiment_id, None))
        return BrowserActionResponse(
            operation=request.operation,
            status="running",
            session_id=payload.session_id,
            experiment_id=experiment_id,
            result={
                "experiment": manifest,
                "manifest_relative_path": (
                    Path("experiments") / experiment_id / "manifest.json"
                ).as_posix(),
                "poll_with": "inspectBrowserEvidence.get_experiment",
            },
        )

    async def _run_capture_job(
        self,
        request: CaptureFlowRequest | CaptureBaselineRequest,
        *,
        deadline: Deadline,
        prepared: tuple[str, Path, dict[str, Any]],
    ) -> None:
        experiment_id = prepared[0]
        try:
            await self._capture_flow(
                request,
                deadline=deadline,
                prepared=prepared,
            )
        except asyncio.CancelledError:
            manifest = self.evidence.load_manifest(experiment_id)
            manifest["status"] = "interrupted"
            manifest["errors"] = [
                *(
                    manifest.get("errors")
                    if isinstance(manifest.get("errors"), list)
                    else []
                ),
                "Background experiment task was canceled during service shutdown.",
            ]
            self.evidence.write_manifest(experiment_id, manifest)
            raise
        except Exception as exc:
            manifest = self.evidence.load_manifest(experiment_id)
            manifest["status"] = "failed"
            manifest["errors"] = [
                *(
                    manifest.get("errors")
                    if isinstance(manifest.get("errors"), list)
                    else []
                ),
                str(exc)[:4000],
            ]
            self.evidence.write_manifest(experiment_id, manifest)

    async def wait_for_job(self, experiment_id: str) -> None:
        task = self._jobs.get(experiment_id)
        if task is not None:
            await task

    async def _capture_flow(
        self,
        request: CaptureFlowRequest | CaptureBaselineRequest,
        *,
        deadline: Deadline | None = None,
        prepared: tuple[str, Path, dict[str, Any]] | None = None,
    ) -> BrowserActionResponse:
        payload = request.payload
        deadline = deadline or Deadline(payload.deadline_ms)
        session_id = payload.session_id
        if prepared is None:
            experiment_id, experiment_dir, manifest = self.evidence.create_experiment(
                session_id=session_id,
                operation=request.operation,
                objective=payload.objective,
                deadline=deadline,
            )
            manifest["execution_mode"] = "sync"
            manifest["primary_request_matcher"] = payload.primary_request.model_dump(
                mode="json", exclude_none=True
            )
            self.evidence.write_manifest(experiment_id, manifest)
        else:
            experiment_id, experiment_dir, manifest = prepared
        async with self._session_lock(session_id):
            session = self._get_session(session_id)
            if session.get("status") != "open":
                manifest["status"] = "failed"
                manifest["errors"] = ["Browser session is not open."]
                self.evidence.write_manifest(experiment_id, manifest)
                return BrowserActionResponse(
                    operation=request.operation,
                    status="failed",
                    session_id=session_id,
                    experiment_id=experiment_id,
                    result={"experiment": manifest},
                    errors=manifest["errors"],
                )
            try:
                alignment = await self._align_session(session, payload, deadline)
            except Exception as exc:
                manifest["status"] = "failed"
                manifest["errors"] = [str(exc)[:4000]]
                self.evidence.write_manifest(experiment_id, manifest)
                return BrowserActionResponse(
                    operation=request.operation,
                    status="failed",
                    session_id=session_id,
                    experiment_id=experiment_id,
                    result={"experiment": manifest},
                    errors=manifest["errors"],
                )
            manifest["page_alignment"] = asdict(alignment)
            manifest["primary_request_matcher"] = payload.primary_request.model_dump(
                mode="json", exclude_none=True
            )
            capture_id: int | None = None
            start_payload: dict[str, Any] = {}
            final_status_payload: dict[str, Any] = {}
            stop_payload: dict[str, Any] = {}
            wait_result: dict[str, Any] | None = None
            trace_paths: list[str] = []
            screenshot_paths: list[str] = []
            network_payload: dict[str, Any] = {}
            step_results: list[FlowStepResult] = []
            wait_observations: list[dict[str, Any]] = []
            errors: list[str] = []
            warnings = list(alignment.warnings)
            trace_started = False
            collector_started = False
            collector_stopped = False
            last_stream_version = 0
            try:
                if payload.capture.trace:
                    await self.playwright.start_trace(
                        session_id,
                        self._operation_deadline(deadline, 3_000, "trace start"),
                    )
                    trace_started = True
                if payload.capture.stream:
                    start_payload = await self.js_reverse.start_stream_capture(
                        experiment_id=experiment_id,
                        matcher=self._request_matcher(payload),
                        include_in_flight=payload.primary_request.include_in_flight,
                        deadline=self._operation_deadline(
                            deadline,
                            5_000,
                            "stream capture start",
                        ),
                    )
                    capture = start_payload.get("capture")
                    if not isinstance(capture, dict) or not capture.get("captureId"):
                        raise BrowserServiceError(
                            "stream_start_invalid", "Stream collector returned no capture ID", 502
                        )
                    capture_id = int(capture["captureId"])
                    collector_started = True
                if payload.capture.screenshots:
                    try:
                        screenshot_paths.append(
                            await self.playwright.capture_screenshot(
                                session_id,
                                experiment_dir,
                                "before-flow",
                                self._operation_deadline(
                                    deadline,
                                    3_000,
                                    "initial screenshot",
                                ),
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"initial screenshot: {str(exc)[:3500]}")
                for step_index, step in enumerate(payload.flow):
                    self._ensure_finalize_reserve(deadline, f"step {step.step_id}")
                    started = utc_now()
                    try:
                        step_deadline = self._operation_deadline(
                            deadline,
                            step.timeout_ms,
                            f"step {step.step_id}",
                        )
                        if step.action in {"wait", "assert"}:
                            result = await self._wait_condition(
                                session_ref=session_id,
                                capture_id=capture_id,
                                condition=step.condition or WaitCondition(type="timeout"),
                                since_version=last_stream_version,
                                deadline=step_deadline,
                            )
                            last_stream_version = max(
                                last_stream_version,
                                int(result.get("capture_version", 0) or 0),
                            )
                            wait_observations.append(
                                {
                                    "step_id": step.step_id,
                                    "step_index": step_index,
                                    "condition_type": (
                                        step.condition.type if step.condition else "timeout"
                                    ),
                                    "capture_version": result.get("capture_version"),
                                    "matched_request_ids": result.get(
                                        "matched_request_ids", []
                                    ),
                                    "matched_event": result.get("matched_event"),
                                    "terminal_status": result.get("terminal_status"),
                                }
                            )
                            if not result.get("condition_met", True):
                                raise BrowserServiceError(
                                    (
                                        "assertion_failed"
                                        if step.action == "assert"
                                        else "wait_condition_timeout"
                                    ),
                                    f"Condition failed: {step.step_id}",
                                    409,
                                )
                            snapshot_ref = None
                        else:
                            result = await self.playwright.execute_step(
                                session_id,
                                step,
                                experiment_dir,
                                step_deadline,
                            )
                            raw_snapshot_ref = result.get("snapshot_ref")
                            snapshot_ref = (
                                self.evidence.relative_path(str(raw_snapshot_ref))
                                if raw_snapshot_ref
                                else None
                            )
                        step_results.append(
                            FlowStepResult(
                                step_id=step.step_id,
                                status="completed",
                                started_at=started,
                                ended_at=utc_now(),
                                snapshot_ref=snapshot_ref,
                            )
                        )
                    except Exception as exc:
                        timed_out = isinstance(exc, BrowserServiceError) and exc.code in {
                            "deadline_exceeded",
                            "deadline_finalize_reserve",
                            "wait_condition_timeout",
                        }
                        step_results.append(
                            FlowStepResult(
                                step_id=step.step_id,
                                status="timed_out" if timed_out else "failed",
                                started_at=started,
                                ended_at=utc_now(),
                                error=str(exc)[:4000],
                            )
                        )
                        raise
                if payload.wait_for:
                    wait_deadline = self._operation_deadline(
                        deadline,
                        payload.wait_for.timeout_ms,
                        "final wait condition",
                    )
                    wait_result = await self._wait_condition(
                        session_ref=session_id,
                        capture_id=capture_id,
                        condition=payload.wait_for,
                        since_version=last_stream_version,
                        deadline=wait_deadline,
                    )
                    last_stream_version = max(
                        last_stream_version,
                        int(wait_result.get("capture_version", 0) or 0),
                    )
                    wait_observations.append(
                        {
                            "step_id": "__final_wait__",
                            "step_index": len(payload.flow),
                            "condition_type": payload.wait_for.type,
                            "capture_version": wait_result.get("capture_version"),
                            "matched_request_ids": wait_result.get(
                                "matched_request_ids", []
                            ),
                            "matched_event": wait_result.get("matched_event"),
                            "terminal_status": wait_result.get("terminal_status"),
                        }
                    )
                    final_status_payload = dict(wait_result.get("status_payload") or {})
            except Exception as exc:
                errors.append(str(exc)[:4000])
            finally:
                if capture_id is not None:
                    try:
                        stop_payload = await self.js_reverse.stop_stream_capture(
                            capture_id, deadline
                        )
                        collector_stopped = True
                        if deadline.remaining_seconds() > 0.1:
                            final_status_payload = await self.js_reverse.get_stream_status(
                                capture_id, deadline
                            )
                    except Exception as exc:
                        errors.append(f"stream finalize: {str(exc)[:3500]}")
                if trace_started:
                    try:
                        trace_paths = await self.playwright.stop_trace(
                            session_id, experiment_dir, deadline
                        )
                    except Exception as exc:
                        warnings.append(f"trace finalize: {str(exc)[:3500]}")
                if payload.capture.network and deadline.remaining_seconds() > 0.1:
                    try:
                        network_payload = await self.js_reverse.list_network_requests(
                            self._request_matcher(payload),
                            deadline,
                        )
                    except Exception as exc:
                        warnings.append(f"network summary: {str(exc)[:3500]}")
                if payload.capture.screenshots and deadline.remaining_seconds() > 0.1:
                    try:
                        screenshot_paths.append(
                            await self.playwright.capture_screenshot(
                                session_id,
                                experiment_dir,
                                "after-flow",
                                deadline.child(min(3_000, deadline.remaining_ms())),
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"final screenshot: {str(exc)[:3500]}")

            primary_requests, primary_integrity, count_ok = self._primary_result(
                payload,
                final_status_payload,
                network_payload,
            )
            cancellation_classifications = self._classify_cancellations(
                payload,
                step_results,
                primary_requests,
                alignment,
                wait_observations,
            )
            capture_summary = (
                final_status_payload.get("capture")
                if isinstance(final_status_payload.get("capture"), dict)
                else {}
            )
            collector_integrity = str(
                capture_summary.get("collectorIntegrity")
                or capture_summary.get("integrityStatus")
                or ("partial" if collector_started else "failed")
            )
            wait_met = wait_result is None or bool(wait_result.get("condition_met"))
            steps_ok = all(item.status == "completed" for item in step_results)
            objective_ok = (
                not errors
                and steps_ok
                and count_ok
                and wait_met
                and (not payload.capture.stream or collector_stopped)
                and primary_integrity != "failed"
                and (
                    payload.primary_request.allow_supporting_failures
                    or collector_integrity != "failed"
                )
            )
            objective_integrity = "complete" if objective_ok else "failed"
            capture_health = {
                "page_aligned": alignment.status == "aligned",
                "stream_collector_started_before_flow": collector_started
                or not payload.capture.stream,
                "pre_arm_requests_excluded": not payload.primary_request.include_in_flight,
                "primary_request_match_count_ok": count_ok,
                "raw_capture_integrity": [
                    item.get("rawCaptureIntegrity") for item in primary_requests
                ],
                "semantic_parse_integrity": [
                    item.get("semanticParseIntegrity") for item in primary_requests
                ],
                "request_snapshot_integrity": [
                    item.get("requestSnapshotIntegrity") for item in primary_requests
                ],
                "artifact_integrity": [
                    item.get("artifactIntegrity") for item in primary_requests
                ],
                "wait_condition_met": wait_met,
                "collector_stopped": collector_stopped or not payload.capture.stream,
                "manifest_written": True,
                "capture_scope": capture_summary.get("captureScope", "page-target-only"),
                "worker_coverage": capture_summary.get("workerCoverage", False),
            }
            artifacts = self._collect_artifacts(
                start_payload,
                final_status_payload,
                stop_payload,
                network_payload,
            )
            for index, screenshot_path in enumerate(screenshot_paths, start=1):
                descriptor = self.evidence.describe_local_artifact(
                    screenshot_path,
                    artifact_id=f"art_{experiment_id}_screenshot_{index}",
                    kind="playwright_screenshot",
                    sensitivity="private",
                )
                if descriptor:
                    artifacts.append(descriptor)
            for index, trace_path in enumerate(trace_paths, start=1):
                descriptor = self.evidence.describe_local_artifact(
                    trace_path,
                    artifact_id=f"art_{experiment_id}_trace_{index}",
                    kind="playwright_trace",
                    sensitivity="private",
                )
                if descriptor:
                    artifacts.append(descriptor)
            relative_screenshot_paths = [
                relative
                for path in screenshot_paths
                if (relative := self.evidence.relative_path(path)) is not None
            ]
            relative_trace_paths = [
                relative
                for path in trace_paths
                if (relative := self.evidence.relative_path(path)) is not None
            ]
            manifest.update(
                {
                    "status": "completed" if objective_ok else "failed",
                    "deadline": deadline.to_dict(),
                    "steps": [item.model_dump(mode="json") for item in step_results],
                    "stream_capture_id": capture_id,
                    "stream_wait_result": wait_result,
                    "wait_observations": wait_observations,
                    "collector_integrity": collector_integrity,
                    "primary_request_integrity": primary_integrity,
                    "objective_integrity": objective_integrity,
                    "primary_requests": primary_requests,
                    "cancellation_classifications": cancellation_classifications,
                    "capture_health": capture_health,
                    "network_summary": network_payload,
                    "screenshot_paths": relative_screenshot_paths,
                    "trace_paths": relative_trace_paths,
                    "artifacts": artifacts,
                    "warnings": warnings,
                    "errors": errors,
                }
            )
            self.evidence.write_manifest(experiment_id, manifest)
            return BrowserActionResponse(
                operation=request.operation,
                status="completed" if objective_ok else "failed",
                session_id=session_id,
                experiment_id=experiment_id,
                result={
                    "experiment": manifest,
                    "manifest_relative_path": (
                        Path("experiments") / experiment_id / "manifest.json"
                    ).as_posix(),
                },
                warnings=warnings,
                errors=errors,
            )

    async def close(self) -> None:
        jobs = list(self._jobs.values())
        for task in jobs:
            task.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        await self.js_reverse.close()


def build_browser_service_from_environment() -> BrowserActionService:
    evidence_root = Path(
        env_value_from_environment_or_dotenv("WEB_REV_EVIDENCE_DIR")
        or "data/analysis-workspace"
    )
    evidence = LocalEvidenceStore(evidence_root)
    browser_endpoint = env_value_from_environment_or_dotenv("WEB_REV_BROWSER_CDP_URL")
    playwright = PlaywrightCliAdapter(
        executable=(
            env_value_from_environment_or_dotenv("WEB_REV_PLAYWRIGHT_CLI")
            or "playwright-cli"
        ),
        cwd=evidence.root,
    )
    command = (
        env_value_from_environment_or_dotenv("WEB_REV_JS_REVERSE_COMMAND")
        or "js-reverse-mcp"
    )
    raw_args = env_value_from_environment_or_dotenv("WEB_REV_JS_REVERSE_ARGS")
    if raw_args:
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise RuntimeError("WEB_REV_JS_REVERSE_ARGS must be a JSON array") from exc
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise RuntimeError("WEB_REV_JS_REVERSE_ARGS must be a JSON string array")
    else:
        args = [
            "--allowedRoots",
            str(evidence.root),
            "--streamArtifactRoot",
            "0",
        ]
        if browser_endpoint:
            args[0:0] = ["--browserUrl", browser_endpoint]
    transport: McpToolTransport = StdioMcpToolTransport(
        command=command,
        args=args,
        cwd=evidence.root,
    )
    js_reverse = JsReverseMcpAdapter(transport)
    return BrowserActionService(
        playwright=playwright,
        js_reverse=js_reverse,
        evidence=evidence,
        default_browser_endpoint=browser_endpoint,
        private_mcp_browser_endpoint=browser_endpoint,
        require_private_mcp_endpoint=True,
    )
