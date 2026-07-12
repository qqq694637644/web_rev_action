"""Atomic browser experiment orchestration and workspace evidence storage."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
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
    StreamCheckpoint,
    StreamRequestCheckpoint,
)
from .browser_models import (
    BrowserActionResponse,
    CaptureBaselineRequest,
    CaptureFlowPayload,
    CaptureFlowRequest,
    CloseSessionRequest,
    FlowStepResult,
    GetExperimentRequest,
    GetSessionRequest,
    GetStreamStatusRequest,
    InspectBrowserEvidenceRequest,
    ListExperimentsRequest,
    OpenSessionRequest,
    RequestMatcher,
    RunBrowserExperimentRequest,
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
    FINALIZE_RESERVE_MS = 5_000
    FINALIZE_GRACE_MS = 8_000
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
        experiments: ExperimentStore,
        default_browser_endpoint: str | None = None,
        private_mcp_browser_endpoint: str | None = None,
        require_private_mcp_endpoint: bool = False,
    ) -> None:
        self.playwright = playwright
        self.js_reverse = js_reverse
        self.experiments = experiments
        self.default_browser_endpoint = default_browser_endpoint
        self.private_mcp_browser_endpoint = private_mcp_browser_endpoint
        self.require_private_mcp_endpoint = require_private_mcp_endpoint
        self.service_instance_id = f"svc_{uuid.uuid4().hex}"
        self.process_started_at = utc_now()
        self.sessions: dict[str, dict[str, Any]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._browser_lock = asyncio.Lock()
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._active_session_jobs: dict[str, str] = {}
        self._active_browser_experiment_id: str | None = None
        self._active_browser_session_id: str | None = None

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    @asynccontextmanager
    async def _locked_browser_session(
        self,
        session_id: str,
        deadline: Deadline,
    ) -> Any:
        browser_acquired = False
        session_acquired = False
        session_lock = self._session_lock(session_id)
        try:
            await asyncio.wait_for(
                self._browser_lock.acquire(),
                timeout=max(0.1, deadline.remaining_seconds()),
            )
            browser_acquired = True
            await asyncio.wait_for(
                session_lock.acquire(),
                timeout=max(0.1, deadline.remaining_seconds()),
            )
            session_acquired = True
            yield
        except TimeoutError as exc:
            raise BrowserServiceError(
                "browser_busy",
                "Timed out waiting for the shared browser experiment lock.",
                409,
            ) from exc
        finally:
            if session_acquired:
                session_lock.release()
            if browser_acquired:
                self._browser_lock.release()

    def _active_job_for_session(self, session_id: str) -> str | None:
        experiment_id = self._active_session_jobs.get(session_id)
        if experiment_id is None:
            return None
        task = self._jobs.get(experiment_id)
        if task is None or task.done():
            self._active_session_jobs.pop(session_id, None)
            return None
        return experiment_id

    def _assert_browser_idle(self) -> None:
        if self._active_browser_experiment_id is None:
            return
        raise BrowserServiceError(
            "browser_busy",
            "The shared browser already has an active experiment: "
            f"{self._active_browser_experiment_id} "
            f"(session {self._active_browser_session_id}).",
            409,
        )

    def _reserve_browser_experiment(
        self,
        *,
        session_id: str,
        experiment_id: str,
    ) -> None:
        self._assert_browser_idle()
        self._active_browser_experiment_id = experiment_id
        self._active_browser_session_id = session_id

    def _release_browser_experiment(self, experiment_id: str) -> None:
        if self._active_browser_experiment_id != experiment_id:
            return
        self._active_browser_experiment_id = None
        self._active_browser_session_id = None

    @staticmethod
    def _manifest_relative_path(experiment_id: str) -> str:
        return (Path("experiments") / experiment_id / "manifest.json").as_posix()

    @staticmethod
    def _experiment_summary(manifest: dict[str, Any]) -> dict[str, Any]:
        primary_requests = manifest.get("primary_requests")
        request_summaries: list[dict[str, Any]] = []
        if isinstance(primary_requests, list):
            for request in primary_requests[:10]:
                if not isinstance(request, dict):
                    continue
                request_summaries.append(
                    {
                        "cdp_request_id": request.get("cdpRequestId"),
                        "persistent_request_id": request.get("persistentRequestId"),
                        "url": str(request.get("url", ""))[:2048],
                        "method": request.get("method"),
                        "status": request.get("status"),
                        "integrity_status": request.get("integrityStatus"),
                        "raw_capture_integrity": request.get(
                            "rawCaptureIntegrity"
                        ),
                        "semantic_parse_integrity": request.get(
                            "semanticParseIntegrity"
                        ),
                        "request_snapshot_integrity": request.get(
                            "requestSnapshotIntegrity"
                        ),
                        "artifact_integrity": request.get("artifactIntegrity"),
                    }
                )
        health = manifest.get("capture_health")
        return {
            "experiment_id": manifest.get("experiment_id"),
            "session_id": manifest.get("session_id"),
            "operation": manifest.get("operation"),
            "status": manifest.get("status"),
            "objective_integrity": manifest.get("objective_integrity"),
            "collector_integrity": manifest.get("collector_integrity"),
            "primary_request_integrity": manifest.get(
                "primary_request_integrity"
            ),
            "primary_integrity_dimensions": manifest.get(
                "primary_integrity_dimensions"
            ),
            "primary_requests": request_summaries,
            "primary_request_count": (
                len(primary_requests) if isinstance(primary_requests, list) else 0
            ),
            "capture_health": dict(health) if isinstance(health, dict) else {},
            "warnings": [str(item)[:1000] for item in manifest.get("warnings", [])[:10]],
            "errors": [str(item)[:1000] for item in manifest.get("errors", [])[:10]],
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
        }

    def _get_session(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id) or self.experiments.load_session(session_id)
        if not session:
            raise BrowserServiceError("session_not_found", "Browser session was not found", 404)
        if (
            session.get("status") == "open"
            and session.get("service_instance_id") != self.service_instance_id
        ):
            session["status"] = "stale"
            session["stale_reason"] = "service_instance_changed"
            session["updated_at"] = utc_now()
            self.experiments.save_session(session)
        self.sessions[session_id] = session
        return session

    async def run(self, request: RunBrowserExperimentRequest) -> BrowserActionResponse:
        if isinstance(request, OpenSessionRequest):
            self._assert_browser_idle()
            return await self._open_session(request)
        if isinstance(request, CloseSessionRequest):
            self._assert_browser_idle()
            return await self._close_session(request)
        if isinstance(request, (CaptureFlowRequest, CaptureBaselineRequest)):
            active_session_experiment = self._active_job_for_session(
                request.payload.session_id
            )
            if active_session_experiment is not None:
                raise BrowserServiceError(
                    "session_busy",
                    "The browser session already has an active background experiment: "
                    f"{active_session_experiment}",
                    409,
                )
            self._assert_browser_idle()
            if request.payload.execution_mode == "job":
                return self._start_capture_job(request)
            payload = request.payload
            deadline = Deadline(payload.deadline_ms)
            experiment_id, experiment_dir, manifest = self.experiments.create_experiment(
                session_id=payload.session_id,
                operation=request.operation,
                objective=payload.objective,
                deadline=deadline,
            )
            manifest["execution_mode"] = "sync"
            manifest["primary_request_matcher"] = payload.primary_request.model_dump(
                mode="json", exclude_none=True
            )
            self.experiments.write_manifest(experiment_id, manifest)
            self._reserve_browser_experiment(
                session_id=payload.session_id,
                experiment_id=experiment_id,
            )
            try:
                return await self._capture_flow(
                    request,
                    deadline=deadline,
                    prepared=(experiment_id, experiment_dir, manifest),
                )
            finally:
                self._release_browser_experiment(experiment_id)
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
            items = self.experiments.list_experiments(
                request.payload.session_id, request.payload.limit
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                result={"experiments": items, "count": len(items)},
            )
        if isinstance(request, GetExperimentRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
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
                result={
                    "experiment": self._experiment_summary(manifest),
                    "manifest_relative_path": self._manifest_relative_path(
                        request.payload.experiment_id
                    ),
                },
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
        async with self._locked_browser_session(session_id, deadline):
            page = await self.playwright.open_session(
                session_id, endpoint, payload.target.start_url, deadline
            )
            if (
                payload.target.page_index is not None
                and payload.target.page_index != page.page_index
            ):
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
                "service_instance_id": self.service_instance_id,
                "process_started_at": self.process_started_at,
                "created_at": now,
                "updated_at": now,
            }
            self.sessions[session_id] = session
            self.experiments.save_session(session)
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
        async with self._locked_browser_session(session_id, deadline):
            session = self._get_session(session_id)
            if session.get("status") == "open":
                await self.playwright.close_session(session_id, deadline)
            session["status"] = "closed"
            session["updated_at"] = utc_now()
            self.experiments.save_session(session)
        return BrowserActionResponse(
            operation=request.operation,
            status="completed",
            session_id=session_id,
            result={"session": session},
        )

    async def _align_session(
        self, session: dict[str, Any], payload: CaptureFlowPayload, deadline: Deadline
    ) -> AlignmentResult:
        selected_index = (
            payload.target.page_index
            if payload.target.page_index is not None
            else int(session.get("playwright_page_index", 0))
        )
        page = await self.playwright.select_page(
            str(session["playwright_session_ref"]),
            selected_index,
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
        self.experiments.save_session(session)
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
        checkpoint: StreamCheckpoint,
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
                checkpoint=checkpoint,
                deadline=condition_deadline,
            )
            return asdict(result)
        return await self.playwright.wait_for_page_condition(
            session_ref,
            condition,
            condition_deadline,
        )

    async def _stream_checkpoint(
        self,
        capture_id: int | None,
        matcher: RequestMatcher,
        deadline: Deadline,
    ) -> StreamCheckpoint:
        if capture_id is None:
            return StreamCheckpoint()
        status = await self.js_reverse.get_stream_status(capture_id, deadline)
        return JsReverseMcpAdapter.checkpoint_from_status(status, matcher)

    @staticmethod
    def _checkpoint_from_wait_result(
        result: dict[str, Any],
        fallback: StreamCheckpoint,
    ) -> StreamCheckpoint:
        value = result.get("checkpoint")
        if not isinstance(value, dict):
            return fallback
        requests_value = value.get("requests")
        requests: dict[str, StreamRequestCheckpoint] = {}
        if isinstance(requests_value, dict):
            for request_id, request_value in requests_value.items():
                if not isinstance(request_value, dict):
                    continue
                requests[str(request_id)] = StreamRequestCheckpoint(
                    response_observed=bool(
                        request_value.get("response_observed", False)
                    ),
                    status=(
                        str(request_value["status"])
                        if request_value.get("status") is not None
                        else None
                    ),
                    terminal_wall_time_ms=(
                        float(request_value["terminal_wall_time_ms"])
                        if isinstance(
                            request_value.get("terminal_wall_time_ms"),
                            (int, float),
                        )
                        else None
                    ),
                    raw_event_index=(
                        int(request_value["raw_event_index"])
                        if isinstance(request_value.get("raw_event_index"), int)
                        else -1
                    ),
                    semantic_event_index=(
                        int(request_value["semantic_event_index"])
                        if isinstance(request_value.get("semantic_event_index"), int)
                        else -1
                    ),
                    primary_event_source=str(
                        request_value.get("primary_event_source") or "none"
                    ),
                )
        return StreamCheckpoint(
            version=max(
                fallback.version,
                int(value.get("version", result.get("capture_version", 0)) or 0),
            ),
            requests=requests or fallback.requests,
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
    ) -> tuple[list[dict[str, Any]], str, bool, dict[str, str]]:
        matcher = self._request_matcher(payload)
        requests = [
            item
            for item in status_payload.get("requests", [])
            if isinstance(item, dict)
            and JsReverseMcpAdapter._request_matches(item, matcher)
        ]
        if not requests and not payload.capture.stream:
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
            if payload.primary_request.expected_min_matches == 0:
                return requests, "complete", count_ok, {
                    "raw_capture": "complete",
                    "semantic_parse": "complete",
                    "request_snapshot": "complete",
                    "artifacts": "complete",
                }
            integrity = "failed"
            return requests, integrity, count_ok, {
                "raw_capture": "failed" if payload.capture.stream else "partial",
                "semantic_parse": "failed" if payload.capture.stream else "partial",
                "request_snapshot": "failed" if payload.capture.stream else "partial",
                "artifacts": "failed" if payload.capture.stream else "partial",
            }
        integrity = max(
            (str(item.get("integrityStatus", "partial")) for item in requests),
            key=self._integrity_severity,
        )
        fields = {
            "raw_capture": "rawCaptureIntegrity",
            "semantic_parse": "semanticParseIntegrity",
            "request_snapshot": "requestSnapshotIntegrity",
            "artifacts": "artifactIntegrity",
        }
        dimensions = {
            name: max(
                (str(item.get(field, "partial")) for item in requests),
                key=self._integrity_severity,
            )
            for name, field in fields.items()
        }
        return requests, integrity, count_ok, dimensions

    @staticmethod
    def _classify_cancellations(
        payload: CaptureFlowPayload,
        step_results: list[FlowStepResult],
        primary_requests: list[dict[str, Any]],
        initial_alignment: AlignmentResult,
        post_alignment: AlignmentResult,
        wait_observations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        completed_by_id = {
            result.step_id: result
            for result in step_results
            if result.status == "completed"
        }
        stop_candidates: list[dict[str, Any]] = []
        for index, step in enumerate(payload.flow):
            if (
                getattr(step, "intent", None) != "stop_generation"
                or step.step_id not in completed_by_id
            ):
                continue
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
            stop_candidates.append(
                {
                    "step_id": step.step_id,
                    "stop_wall_ms": stop_wall_ms,
                    "before": before_observation,
                    "after": after_observation,
                }
            )
        page_remained_aligned = (
            initial_alignment.status == "aligned"
            and post_alignment.status == "aligned"
            and initial_alignment.js_reverse_page_id
            == post_alignment.js_reverse_page_id
            and initial_alignment.playwright_page.url
            == post_alignment.playwright_page.url
        )
        classifications: list[dict[str, Any]] = []
        for request in primary_requests:
            if request.get("status") != "canceled":
                continue
            if request.get("terminalReason") != "network_canceled":
                continue
            ended_wall_ms = request.get("endedWallTimeMs")
            if not isinstance(ended_wall_ms, (int, float)) or not stop_candidates:
                continue
            nearest = min(
                stop_candidates,
                key=lambda item: abs(ended_wall_ms - int(item["stop_wall_ms"])),
            )
            delta_ms = ended_wall_ms - int(nearest["stop_wall_ms"])
            within_window = -500 <= delta_ms <= 5_000
            request_ids = {
                str(request.get("cdpRequestId") or ""),
                str(request.get("persistentRequestId") or ""),
            }
            before_ids = set((nearest.get("before") or {}).get("matched_request_ids", []))
            after_ids = set((nearest.get("after") or {}).get("matched_request_ids", []))
            same_request_observed = bool(request_ids & before_ids) and bool(
                request_ids & after_ids
            )
            expected = within_window and page_remained_aligned and same_request_observed
            classification = {
                "request_id": request.get("cdpRequestId"),
                "persistent_request_id": request.get("persistentRequestId"),
                "source_terminal_reason": "network_canceled",
                "classification": (
                    "expected_user_cancel"
                    if expected
                    else "unclassified_network_cancel"
                ),
                "stop_step_id": nearest["step_id"],
                "stop_delta_ms": delta_ms,
                "within_stop_window": within_window,
                "page_remained_aligned": page_remained_aligned,
                "same_request_observed": same_request_observed,
                "stream_before_stop": (
                    (nearest.get("before") or {}).get("matched_event")
                ),
                "stream_after_stop": (
                    (nearest.get("after") or {}).get("matched_event")
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
        active_experiment = self._active_job_for_session(payload.session_id)
        if active_experiment is not None:
            raise BrowserServiceError(
                "session_busy",
                "The browser session already has an active background experiment: "
                f"{active_experiment}",
                409,
            )
        deadline = Deadline(payload.job_timeout_ms)
        experiment_id, experiment_dir, manifest = self.experiments.create_experiment(
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
        self.experiments.write_manifest(experiment_id, manifest)
        self._reserve_browser_experiment(
            session_id=payload.session_id,
            experiment_id=experiment_id,
        )
        task = asyncio.create_task(
            self._run_capture_job(
                request,
                deadline=deadline,
                prepared=(experiment_id, experiment_dir, manifest),
            ),
            name=f"browser-experiment-{experiment_id}",
        )
        self._jobs[experiment_id] = task
        self._active_session_jobs[payload.session_id] = experiment_id

        def clear_job(_task: asyncio.Task[None]) -> None:
            self._jobs.pop(experiment_id, None)
            if self._active_session_jobs.get(payload.session_id) == experiment_id:
                self._active_session_jobs.pop(payload.session_id, None)
            self._release_browser_experiment(experiment_id)

        task.add_done_callback(clear_job)
        return BrowserActionResponse(
            operation=request.operation,
            status="running",
            session_id=payload.session_id,
            experiment_id=experiment_id,
            result={
                "experiment": self._experiment_summary(manifest),
                "manifest_relative_path": self._manifest_relative_path(experiment_id),
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
            manifest = self.experiments.load_manifest(experiment_id)
            manifest["status"] = "interrupted"
            manifest["errors"] = [
                *(
                    manifest.get("errors")
                    if isinstance(manifest.get("errors"), list)
                    else []
                ),
                "Background experiment task was canceled during service shutdown.",
            ]
            self.experiments.write_manifest(experiment_id, manifest)
            raise
        except Exception as exc:
            manifest = self.experiments.load_manifest(experiment_id)
            manifest["status"] = "failed"
            manifest["errors"] = [
                *(
                    manifest.get("errors")
                    if isinstance(manifest.get("errors"), list)
                    else []
                ),
                str(exc)[:4000],
            ]
            self.experiments.write_manifest(experiment_id, manifest)

    async def wait_for_job(self, experiment_id: str) -> None:
        task = self._jobs.get(experiment_id)
        if task is not None:
            await task

    async def _finalize_experiment_runtime(
        self,
        *,
        session_id: str,
        experiment_dir: Path,
        payload: CaptureFlowPayload,
        capture_id: int | None,
        trace_started: bool,
        execution_deadline: Deadline,
    ) -> dict[str, Any]:
        cleanup_deadline = Deadline(self.FINALIZE_GRACE_MS)
        entered_reserve = execution_deadline.remaining_ms() <= self.FINALIZE_RESERVE_MS
        result: dict[str, Any] = {
            "stop_payload": {},
            "final_status_payload": {},
            "trace_paths": [],
            "screenshot_paths": [],
            "network_payload": {},
            "collector_stopped": capture_id is None,
            "collector_cleanup": (
                "not_required" if capture_id is None else "unknown"
            ),
            "orphan_capture_id": None,
            "warnings": [],
            "errors": [],
            "entered_finalize_reserve": entered_reserve,
        }
        if capture_id is not None:
            try:
                result["stop_payload"] = await self.js_reverse.stop_stream_capture(
                    capture_id,
                    cleanup_deadline.child(6_000),
                )
                result["collector_stopped"] = True
                result["collector_cleanup"] = "completed"
                if cleanup_deadline.remaining_ms() > 500:
                    result["final_status_payload"] = (
                        await self.js_reverse.get_stream_status(
                            capture_id,
                            cleanup_deadline.child(1_500),
                        )
                    )
            except Exception as exc:
                result["errors"].append(f"stream finalize: {str(exc)[:3500]}")
                result["orphan_capture_id"] = capture_id
                message = str(exc).lower()
                result["collector_cleanup"] = (
                    "timed_out"
                    if "timed out" in message or "deadline" in message
                    else "unknown"
                )
        if trace_started:
            try:
                result["trace_paths"] = await self.playwright.stop_trace(
                    session_id,
                    experiment_dir,
                    cleanup_deadline.child(1_500),
                    collect_files=not entered_reserve,
                )
            except Exception as exc:
                result["warnings"].append(f"trace finalize: {str(exc)[:3500]}")
        if not entered_reserve and execution_deadline.remaining_ms() > 1_000:
            if payload.capture.network:
                try:
                    result["network_payload"] = (
                        await self.js_reverse.list_network_requests(
                            self._request_matcher(payload),
                            execution_deadline.child(
                                min(2_000, execution_deadline.remaining_ms())
                            ),
                        )
                    )
                except Exception as exc:
                    result["warnings"].append(
                        f"network summary: {str(exc)[:3500]}"
                    )
            if payload.capture.screenshots and execution_deadline.remaining_ms() > 500:
                try:
                    result["screenshot_paths"].append(
                        await self.playwright.capture_screenshot(
                            session_id,
                            experiment_dir,
                            "after-flow",
                            execution_deadline.child(
                                min(2_000, execution_deadline.remaining_ms())
                            ),
                        )
                    )
                except Exception as exc:
                    result["warnings"].append(
                        f"final screenshot: {str(exc)[:3500]}"
                    )
        return result

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
            experiment_id, experiment_dir, manifest = self.experiments.create_experiment(
                session_id=session_id,
                operation=request.operation,
                objective=payload.objective,
                deadline=deadline,
            )
            manifest["execution_mode"] = "sync"
            manifest["primary_request_matcher"] = payload.primary_request.model_dump(
                mode="json", exclude_none=True
            )
            self.experiments.write_manifest(experiment_id, manifest)
        else:
            experiment_id, experiment_dir, manifest = prepared
        async with self._locked_browser_session(session_id, deadline):
            session = self._get_session(session_id)
            if session.get("status") != "open":
                manifest["status"] = "failed"
                manifest["errors"] = ["Browser session is not open."]
                self.experiments.write_manifest(experiment_id, manifest)
                return BrowserActionResponse(
                    operation=request.operation,
                    status="failed",
                    session_id=session_id,
                    experiment_id=experiment_id,
                    result={
                        "experiment": self._experiment_summary(manifest),
                        "manifest_relative_path": self._manifest_relative_path(
                            experiment_id
                        ),
                    },
                    errors=manifest["errors"],
                )
            try:
                alignment = await self._align_session(session, payload, deadline)
            except asyncio.CancelledError as exc:
                manifest["status"] = "interrupted"
                manifest["errors"] = [
                    "Experiment was canceled during page alignment before flow execution."
                ]
                manifest["interrupted_at"] = utc_now()
                manifest["updated_at"] = utc_now()
                write_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.experiments.write_manifest,
                        experiment_id,
                        manifest,
                    )
                )
                await asyncio.shield(write_task)
                raise exc
            except Exception as exc:
                manifest["status"] = "failed"
                manifest["errors"] = [str(exc)[:4000]]
                self.experiments.write_manifest(experiment_id, manifest)
                return BrowserActionResponse(
                    operation=request.operation,
                    status="failed",
                    session_id=session_id,
                    experiment_id=experiment_id,
                    result={
                        "experiment": self._experiment_summary(manifest),
                        "manifest_relative_path": self._manifest_relative_path(
                            experiment_id
                        ),
                    },
                    errors=manifest["errors"],
                )
            manifest["page_alignment"] = asdict(alignment)
            manifest["primary_request_matcher"] = payload.primary_request.model_dump(
                mode="json", exclude_none=True
            )
            capture_id: int | None = None
            capture_uuid: str | None = None
            capture_relative_dir: str | None = None
            capture_metadata_artifact_id: str | None = None
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
            stream_checkpoint = StreamCheckpoint()
            request_matcher = self._request_matcher(payload)
            collector_start_wall_time_ms: int | None = None
            first_mutation_wall_time_ms: int | None = None
            cancelled_error: asyncio.CancelledError | None = None
            cleanup_result: dict[str, Any] = {}
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
                        matcher=request_matcher,
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
                    capture_uuid = (
                        str(capture["captureUuid"])
                        if capture.get("captureUuid")
                        else None
                    )
                    capture_relative_dir = (
                        str(capture["relativeDir"])
                        if capture.get("relativeDir")
                        else None
                    )
                    metadata_artifact = capture.get("metadataArtifact")
                    if isinstance(metadata_artifact, dict) and metadata_artifact.get(
                        "artifactId"
                    ):
                        capture_metadata_artifact_id = str(
                            metadata_artifact["artifactId"]
                        )
                    collector_started = True
                    collector_start_wall_time_ms = int(
                        capture.get("captureArmedWallTimeMs") or time.time() * 1000
                    )
                    stream_checkpoint = await self._stream_checkpoint(
                        capture_id,
                        request_matcher,
                        self._operation_deadline(
                            deadline,
                            1_500,
                            "initial stream checkpoint",
                        ),
                    )
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
                        if step.action not in {"wait", "assert", "snapshot"}:
                            if capture_id is not None:
                                stream_checkpoint = await self._stream_checkpoint(
                                    capture_id,
                                    request_matcher,
                                    self._operation_deadline(
                                        deadline,
                                        1_500,
                                        f"checkpoint before {step.step_id}",
                                    ),
                                )
                            if first_mutation_wall_time_ms is None:
                                first_mutation_wall_time_ms = int(time.time() * 1000)
                        step_deadline = self._operation_deadline(
                            deadline,
                            step.timeout_ms,
                            f"step {step.step_id}",
                        )
                        if step.action in {"wait", "assert"}:
                            result = await self._wait_condition(
                                session_ref=session_id,
                                capture_id=capture_id,
                                condition=step.condition,
                                checkpoint=stream_checkpoint,
                                deadline=step_deadline,
                            )
                            stream_checkpoint = self._checkpoint_from_wait_result(
                                result,
                                stream_checkpoint,
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
                                self.experiments.relative_path(str(raw_snapshot_ref))
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
                    except asyncio.CancelledError:
                        step_results.append(
                            FlowStepResult(
                                step_id=step.step_id,
                                status="canceled_outcome_unknown",
                                started_at=started,
                                ended_at=utc_now(),
                                error=(
                                    "The local command was canceled and its process tree was "
                                    "terminated. A side effect already delivered to the page "
                                    "cannot be rolled back generically."
                                ),
                            )
                        )
                        raise
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
                        checkpoint=stream_checkpoint,
                        deadline=wait_deadline,
                    )
                    stream_checkpoint = self._checkpoint_from_wait_result(
                        wait_result,
                        stream_checkpoint,
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
            except asyncio.CancelledError as exc:
                cancelled_error = exc
                errors.append("Experiment task was canceled; finalization was attempted.")
            except Exception as exc:
                errors.append(str(exc)[:4000])
            finally:
                cleanup_task = asyncio.create_task(
                    self._finalize_experiment_runtime(
                        session_id=session_id,
                        experiment_dir=experiment_dir,
                        payload=payload,
                        capture_id=capture_id,
                        trace_started=trace_started,
                        execution_deadline=deadline,
                    ),
                    name=f"finalize-{experiment_id}",
                )
                try:
                    cleanup_result = await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cleanup_result = await cleanup_task
                stop_payload = dict(cleanup_result.get("stop_payload") or {})
                cleanup_status = dict(
                    cleanup_result.get("final_status_payload") or {}
                )
                if cleanup_status:
                    final_status_payload = cleanup_status
                trace_paths = list(cleanup_result.get("trace_paths") or [])
                screenshot_paths.extend(
                    str(item)
                    for item in cleanup_result.get("screenshot_paths", [])
                )
                network_payload = dict(
                    cleanup_result.get("network_payload") or {}
                )
                collector_stopped = bool(cleanup_result.get("collector_stopped"))
                warnings.extend(str(item) for item in cleanup_result.get("warnings", []))
                errors.extend(str(item) for item in cleanup_result.get("errors", []))

            post_alignment = AlignmentResult(
                status="not_checked",
                playwright_page=alignment.playwright_page,
                warnings=["Post-flow page alignment was not checked."],
            )
            try:
                post_deadline = Deadline(2_500)
                post_page = await self.playwright.current_page(session_id, post_deadline)
                post_alignment = await self.js_reverse.align_page(
                    post_page,
                    post_deadline,
                    page_id=(
                        str(session["js_reverse_page_id"])
                        if session.get("js_reverse_page_id")
                        else None
                    ),
                )
            except Exception as exc:
                warnings.append(f"post-flow alignment: {str(exc)[:3500]}")

            (
                primary_requests,
                primary_integrity,
                count_ok,
                primary_dimensions,
            ) = self._primary_result(
                payload,
                final_status_payload,
                network_payload,
            )
            cancellation_classifications = self._classify_cancellations(
                payload,
                step_results,
                primary_requests,
                alignment,
                post_alignment,
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
            objective_failed = (
                cancelled_error is not None
                or bool(errors)
                or not steps_ok
                or not count_ok
                or not wait_met
                or (payload.capture.stream and not collector_stopped)
                or primary_integrity == "failed"
                or (
                    not payload.primary_request.allow_supporting_failures
                    and collector_integrity == "failed"
                )
            )
            required_dimensions = {
                "raw_capture": payload.requirements.require_raw_capture,
                "semantic_parse": payload.requirements.require_semantic_parse,
                "request_snapshot": payload.requirements.require_request_snapshot,
                "artifacts": payload.requirements.require_artifacts,
            }
            required_values = [
                primary_dimensions[name]
                for name, required in required_dimensions.items()
                if required and payload.primary_request.expected_min_matches > 0
            ]
            if any(value == "failed" for value in required_values):
                objective_failed = True
            objective_partial = (
                not objective_failed
                and (
                    primary_integrity != "complete"
                    or any(value != "complete" for value in required_values)
                    or (
                        not payload.primary_request.allow_supporting_failures
                        and collector_integrity != "complete"
                    )
                )
            )
            objective_integrity = (
                "failed"
                if objective_failed
                else "partial"
                if objective_partial
                else "complete"
            )
            response_status = (
                "interrupted"
                if cancelled_error is not None
                else "failed"
                if objective_integrity == "failed"
                else "partial"
                if objective_integrity == "partial"
                else "completed"
            )
            pre_arm_request_count = sum(
                1
                for item in primary_requests
                if bool(item.get("requestStartedBeforeCapture"))
            )
            collector_before_mutation = (
                None
                if first_mutation_wall_time_ms is None
                else collector_start_wall_time_ms is not None
                and collector_start_wall_time_ms <= first_mutation_wall_time_ms
            )
            capture_health = {
                "page_aligned_before_flow": alignment.status == "aligned",
                "page_aligned_after_flow": post_alignment.status == "aligned",
                "collector_start_wall_time_ms": collector_start_wall_time_ms,
                "first_mutation_wall_time_ms": first_mutation_wall_time_ms,
                "collector_started_before_first_mutation": collector_before_mutation,
                "include_in_flight_requested": payload.primary_request.include_in_flight,
                "pre_arm_request_count": pre_arm_request_count,
                "primary_request_match_count_ok": count_ok,
                "primary_integrity_dimensions": primary_dimensions,
                "wait_condition_met": wait_met,
                "collector_stopped": collector_stopped or not payload.capture.stream,
                "collector_cleanup": cleanup_result.get(
                    "collector_cleanup",
                    "not_required" if not payload.capture.stream else "unknown",
                ),
                "orphan_capture_id": cleanup_result.get("orphan_capture_id"),
                "capture_uuid": capture_uuid,
                "capture_relative_dir": capture_relative_dir,
                "capture_metadata_artifact_id": capture_metadata_artifact_id,
                "capture_namespace": experiment_id,
                "entered_finalize_reserve": cleanup_result.get(
                    "entered_finalize_reserve", False
                ),
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
                descriptor = self.experiments.describe_local_artifact(
                    screenshot_path,
                    artifact_id=f"art_{experiment_id}_screenshot_{index}",
                    kind="playwright_screenshot",
                    sensitivity="private",
                )
                if descriptor:
                    artifacts.append(descriptor)
            for index, trace_path in enumerate(trace_paths, start=1):
                descriptor = self.experiments.describe_local_artifact(
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
                if (relative := self.experiments.relative_path(path)) is not None
            ]
            relative_trace_paths = [
                relative
                for path in trace_paths
                if (relative := self.experiments.relative_path(path)) is not None
            ]
            manifest.update(
                {
                    "status": response_status,
                    "deadline": deadline.to_dict(),
                    "steps": [item.model_dump(mode="json") for item in step_results],
                    "stream_capture_id": capture_id,
                    "stream_wait_result": wait_result,
                    "wait_observations": wait_observations,
                    "collector_integrity": collector_integrity,
                    "primary_request_integrity": primary_integrity,
                    "objective_integrity": objective_integrity,
                    "objective_requirements": payload.requirements.model_dump(mode="json"),
                    "primary_integrity_dimensions": primary_dimensions,
                    "primary_requests": primary_requests,
                    "cancellation_classifications": cancellation_classifications,
                    "post_flow_alignment": asdict(post_alignment),
                    "capture_health": capture_health,
                    "network_summary": network_payload,
                    "screenshot_paths": relative_screenshot_paths,
                    "trace_paths": relative_trace_paths,
                    "artifacts": artifacts,
                    "warnings": warnings,
                    "errors": errors,
                }
            )
            if cancelled_error is not None:
                manifest["interrupted_at"] = utc_now()
                write_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.experiments.write_manifest,
                        experiment_id,
                        manifest,
                    )
                )
                await asyncio.shield(write_task)
                raise cancelled_error
            self.experiments.write_manifest(experiment_id, manifest)
            return BrowserActionResponse(
                operation=request.operation,
                status=response_status,
                session_id=session_id,
                experiment_id=experiment_id,
                result={
                    "experiment": self._experiment_summary(manifest),
                    "manifest_relative_path": self._manifest_relative_path(
                        experiment_id
                    ),
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
        self._active_session_jobs.clear()
        self._active_browser_experiment_id = None
        self._active_browser_session_id = None
        for session_id, session in list(self.sessions.items()):
            if (
                session.get("status") != "open"
                or session.get("service_instance_id") != self.service_instance_id
            ):
                continue
            deadline = Deadline(5_000)
            try:
                async with self._locked_browser_session(session_id, deadline):
                    await self.playwright.close_session(session_id, deadline)
                    session["status"] = "closed"
                    session["close_reason"] = "service_shutdown"
                    session["updated_at"] = utc_now()
                    self.experiments.save_session(session)
            except Exception:
                session["status"] = "stale"
                session["stale_reason"] = "shutdown_detach_failed"
                session["updated_at"] = utc_now()
                self.experiments.save_session(session)
        await self.js_reverse.close()


def build_browser_service_from_environment() -> BrowserActionService:
    evidence_root = Path(
        env_value_from_environment_or_dotenv("WEB_REV_EVIDENCE_DIR")
        or "data/analysis-workspace"
    )
    experiments = ExperimentStore(evidence_root)
    browser_endpoint = env_value_from_environment_or_dotenv("WEB_REV_BROWSER_CDP_URL")
    playwright = PlaywrightCliAdapter(
        executable=(
            env_value_from_environment_or_dotenv("WEB_REV_PLAYWRIGHT_CLI")
            or "playwright-cli"
        ),
        cwd=experiments.root,
    )
    command = (
        env_value_from_environment_or_dotenv("WEB_REV_JS_REVERSE_COMMAND")
        or "js-reverse-mcp"
    )
    critical_args = [
        "--allowedRoots",
        str(experiments.root),
        "--streamArtifactRoot",
        "0",
    ]
    if browser_endpoint:
        critical_args[0:0] = ["--browserUrl", browser_endpoint]
    raw_args = env_value_from_environment_or_dotenv(
        "WEB_REV_JS_REVERSE_EXTRA_ARGS"
    )
    extra_args: list[str] = []
    if raw_args:
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "WEB_REV_JS_REVERSE_EXTRA_ARGS must be a JSON array"
            ) from exc
        if not isinstance(parsed_args, list) or not all(
            isinstance(item, str) for item in parsed_args
        ):
            raise RuntimeError(
                "WEB_REV_JS_REVERSE_EXTRA_ARGS must be a JSON string array"
            )
        forbidden = {"--browserUrl", "--allowedRoots", "--streamArtifactRoot"}
        for item in parsed_args:
            option = item.split("=", 1)[0]
            if option in forbidden:
                raise RuntimeError(
                    f"{option} is managed by web_rev_action and cannot appear in "
                    "WEB_REV_JS_REVERSE_EXTRA_ARGS"
                )
        extra_args = list(parsed_args)
    args = [*critical_args, *extra_args]
    transport: McpToolTransport = StdioMcpToolTransport(
        command=command,
        args=args,
        cwd=experiments.root,
    )
    js_reverse = JsReverseMcpAdapter(transport)
    return BrowserActionService(
        playwright=playwright,
        js_reverse=js_reverse,
        experiments=experiments,
        default_browser_endpoint=browser_endpoint,
        private_mcp_browser_endpoint=browser_endpoint,
        require_private_mcp_endpoint=True,
    )
