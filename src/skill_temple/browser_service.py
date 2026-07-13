"""Atomic browser experiment orchestration and workspace evidence storage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .browser_adapters import (
    AlignmentResult,
    JsReverseAdapter,
    JsReverseMcpAdapter,
    McpToolCallError,
    McpToolTransport,
    PlaywrightAdapter,
    PlaywrightCliAdapter,
    StdioMcpToolTransport,
    StreamCheckpoint,
    StreamRequestCheckpoint,
)
from .browser_models import (
    BrowserActionResponse,
    CancelExperimentRequest,
    CaptureBaselineRequest,
    CaptureFlowPayload,
    CaptureFlowRequest,
    CloseSessionRequest,
    FlowStepResult,
    GetExperimentRequest,
    GetNetworkEvidenceRequest,
    GetRequestInitiatorRequest,
    GetRequestShapeRequest,
    GetScriptSourceRequest,
    GetSessionRequest,
    GetStreamStatusRequest,
    InspectBrowserEvidenceRequest,
    ListConsoleErrorsRequest,
    ListEvidenceRequest,
    ListExperimentsRequest,
    OpenSessionRequest,
    PrimaryRequest,
    ReplayControlPayload,
    ReplayExploratoryPayload,
    ReplayRequestPayload,
    ReplayRequestRequest,
    ReplayTreatmentPayload,
    RequestMatcher,
    RunBrowserExperimentRequest,
    SaveScriptSourceRequest,
    SearchScriptsRequest,
    VolatileBinding,
    WaitCondition,
)
from .protocol_evidence import (
    assess_control_wire_baseline,
    assess_mutation_effectiveness,
    assess_paired_mutation_effectiveness,
    binding_value_from_snapshot,
    build_replay_spec,
    canonical_json_sha256,
    classify_replay_response,
    evidence_id,
    json_pointer_value,
    load_snapshot,
    network_checkpoint,
    network_request_matches,
    network_snapshot_dimensions,
    public_network_summary,
    redacted_request_body_from_snapshot,
    request_body_canonical_sha256_from_snapshot,
    request_body_canonical_sha256_from_spec,
    request_shape_from_snapshot,
    requests_after_checkpoint,
    response_content_type,
    response_value_from_snapshot,
    select_network_evidence,
    validate_binding_mutation_compatibility,
)
from .runtime import env_value_from_environment_or_dotenv
from .runtime_coordinator import (
    RuntimeCoordinator,
    RuntimeOwner,
    RuntimeReservationError,
)


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
                    "execution_integrity": manifest.get("execution_integrity"),
                    "evidence_integrity": manifest.get("evidence_integrity"),
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
        coordinator: RuntimeCoordinator | None = None,
    ) -> None:
        self.playwright = playwright
        self.js_reverse = js_reverse
        self.experiments = experiments
        self.default_browser_endpoint = default_browser_endpoint
        self.private_mcp_browser_endpoint = private_mcp_browser_endpoint
        self.require_private_mcp_endpoint = require_private_mcp_endpoint
        self.coordinator = coordinator or RuntimeCoordinator()
        self.service_instance_id = f"svc_{uuid.uuid4().hex}"
        self.process_started_at = utc_now()
        self.sessions: dict[str, dict[str, Any]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._browser_lock = asyncio.Lock()
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._active_session_jobs: dict[str, str] = {}

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

    async def _reserve_browser_operation(
        self,
        *,
        session_id: str,
        owner_id: str,
        operation: str,
        experiment_id: str | None = None,
    ) -> None:
        try:
            await self.coordinator.reserve_browser(
                RuntimeOwner(
                    kind="browser",
                    owner_id=owner_id,
                    operation=operation,
                    session_id=session_id,
                    experiment_id=experiment_id,
                )
            )
        except RuntimeReservationError as exc:
            raise BrowserServiceError(exc.code, str(exc), 409) from exc

    async def _release_browser_operation(self, owner_id: str) -> None:
        await self.coordinator.release_browser(owner_id)

    async def _run_aligned_inspection(
        self,
        *,
        session_id: str,
        operation: str,
        callback: Callable[[Deadline], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        owner_id = f"inspect_{uuid.uuid4().hex}"
        await self._reserve_browser_operation(
            session_id=session_id,
            owner_id=owner_id,
            operation=operation,
        )
        deadline = Deadline(15_000)
        try:
            async with self._locked_browser_session(session_id, deadline):
                session = self._get_session(session_id)
                if session.get("status") != "open":
                    raise BrowserServiceError(
                        "session_closed",
                        "Browser session is not open.",
                        409,
                    )
                page = await self.playwright.current_page(session_id, deadline.child(3_000))
                alignment = await self.js_reverse.align_page(
                    page,
                    deadline.child(3_000),
                    page_id=(
                        str(session["js_reverse_page_id"])
                        if session.get("js_reverse_page_id")
                        else None
                    ),
                )
                if alignment.status != "aligned":
                    raise BrowserServiceError(
                        "page_alignment_failed",
                        "Playwright and js-reverse pages are not aligned.",
                        409,
                    )
                return await callback(deadline)
        finally:
            await self._release_browser_operation(owner_id)

    async def _save_script_source(
        self,
        request: SaveScriptSourceRequest,
    ) -> BrowserActionResponse:
        payload = request.payload
        manifest = self.experiments.load_manifest(payload.target_experiment_id)
        if manifest.get("session_id") != payload.session_id:
            raise BrowserServiceError(
                "script_target_session_mismatch",
                "The target experiment belongs to a different browser session.",
                409,
            )
        if payload.initiator_evidence_id:
            initiator = self._find_evidence(
                manifest,
                payload.initiator_evidence_id,
            )
            if initiator.get("kind") != "network_request":
                raise BrowserServiceError(
                    "initiator_evidence_kind_invalid",
                    "initiator_evidence_id must reference network_request evidence.",
                    409,
                )

        async def source(deadline: Deadline) -> dict[str, Any]:
            return await self.js_reverse.get_script_source(
                deadline,
                url=payload.url,
                script_id=payload.script_id,
                start_line=payload.start_line,
                end_line=payload.end_line,
                offset=payload.offset,
                length=payload.length,
            )

        result = await self._run_aligned_inspection(
            session_id=payload.session_id,
            operation=request.operation,
            callback=source,
        )
        source_text = result.get("source") or result.get("scriptSource")
        if not isinstance(source_text, str):
            source_text = json.dumps(result, ensure_ascii=False, indent=2)
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        label = (
            re.sub(
                r"[^A-Za-z0-9_.-]+",
                "-",
                payload.evidence_label or payload.script_id or payload.url or "script",
            ).strip("-._")[:80]
            or "script"
        )
        ev_id = evidence_id(
            payload.target_experiment_id,
            "script_source",
            selector_id=label,
            stable_id=digest[:16],
        )
        source_dir = (
            self.experiments.root
            / "experiments"
            / payload.target_experiment_id
            / "js-reverse"
            / "sources"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        source_file = source_dir / f"{ev_id}.js"
        metadata_file = source_dir / f"{ev_id}.metadata.json"
        source_file.write_text(source_text, encoding="utf-8")
        metadata = {
            "script_url": payload.url,
            "script_id": payload.script_id,
            "start_line": payload.start_line,
            "end_line": payload.end_line,
            "offset": payload.offset,
            "length": payload.length,
            "sha256": digest,
            "initiator_evidence_id": payload.initiator_evidence_id,
        }
        metadata_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        source_artifact_id = f"art_{ev_id}_source"
        metadata_artifact_id = f"art_{ev_id}_metadata"
        source_descriptor = self.experiments.describe_local_artifact(
            str(source_file),
            artifact_id=source_artifact_id,
            kind="script_source",
            sensitivity="private",
        )
        metadata_descriptor = self.experiments.describe_local_artifact(
            str(metadata_file),
            artifact_id=metadata_artifact_id,
            kind="script_source_metadata",
            sensitivity="private",
        )
        artifacts = [item for item in (source_descriptor, metadata_descriptor) if item is not None]
        evidence = {
            "evidence_id": ev_id,
            "kind": "script_source",
            "artifact_ids": [item["artifactId"] for item in artifacts],
            "artifact_paths": {item["kind"]: item["relativePath"] for item in artifacts},
            "initiator_evidence_id": payload.initiator_evidence_id,
            "script_url": payload.url,
            "script_id": payload.script_id,
            "sha256": digest,
            "range": {
                "start_line": payload.start_line,
                "end_line": payload.end_line,
                "offset": payload.offset,
                "length": payload.length,
            },
        }
        self._evidence_index(manifest).append(evidence)
        existing_artifacts = manifest.get("artifacts")
        if not isinstance(existing_artifacts, list):
            existing_artifacts = []
            manifest["artifacts"] = existing_artifacts
        existing_artifacts.extend(artifacts)
        manifest["updated_at"] = utc_now()
        self.experiments.write_manifest(payload.target_experiment_id, manifest)
        return BrowserActionResponse(
            operation=request.operation,
            status="completed",
            session_id=payload.session_id,
            experiment_id=payload.target_experiment_id,
            result={"evidence": evidence},
        )

    @staticmethod
    def _manifest_relative_path(experiment_id: str) -> str:
        return (Path("experiments") / experiment_id / "manifest.json").as_posix()

    def _transport_generation(self) -> int:
        return int(getattr(self.js_reverse, "transport_generation", 0))

    def _discover_capture_metadata(self, experiment_id: str) -> dict[str, Any] | None:
        base = self.experiments.experiment_dir(experiment_id) / "js-reverse"
        candidates = sorted(
            base.glob("capture-*/capture.json"),
            key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
            reverse=True,
        )
        for path in candidates:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            relative = self.experiments.relative_path(str(path.parent))
            return {
                "capture_id": value.get("captureId"),
                "capture_uuid": value.get("captureUuid"),
                "capture_relative_dir": relative,
                "capture_metadata_artifact_id": (
                    (value.get("metadataArtifact") or {}).get("artifactId")
                    if isinstance(value.get("metadataArtifact"), dict)
                    else None
                ),
                "capture_metadata_relative_path": self.experiments.relative_path(str(path)),
            }
        return None

    @staticmethod
    def _manifest_stream_runtime(manifest: dict[str, Any]) -> dict[str, Any]:
        runtime = manifest.get("stream_runtime")
        if isinstance(runtime, dict):
            return dict(runtime)
        health = manifest.get("capture_health")
        health = health if isinstance(health, dict) else {}
        return {
            "capture_id": manifest.get("stream_capture_id"),
            "capture_uuid": health.get("capture_uuid"),
            "capture_relative_dir": health.get("capture_relative_dir"),
            "capture_metadata_artifact_id": health.get("capture_metadata_artifact_id"),
            "transport_generation": health.get("transport_generation"),
            "start_status": health.get("stream_start_status"),
        }

    def _write_stream_runtime(
        self,
        *,
        experiment_id: str,
        manifest: dict[str, Any],
        start_status: str,
        capture_id: int | None,
        capture_uuid: str | None,
        capture_relative_dir: str | None,
        capture_metadata_artifact_id: str | None,
        transport_generation: int | None,
    ) -> None:
        manifest["stream_runtime"] = {
            "start_status": start_status,
            "capture_id": capture_id,
            "capture_uuid": capture_uuid,
            "capture_relative_dir": capture_relative_dir,
            "capture_metadata_artifact_id": capture_metadata_artifact_id,
            "transport_generation": transport_generation,
            "capture_namespace": experiment_id,
        }
        self.experiments.write_manifest(experiment_id, manifest)

    @staticmethod
    def _evidence_index(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        value = manifest.get("evidence")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        manifest["evidence"] = []
        return manifest["evidence"]

    @classmethod
    def _find_evidence(cls, manifest: dict[str, Any], target_evidence_id: str) -> dict[str, Any]:
        for item in cls._evidence_index(manifest):
            if item.get("evidence_id") == target_evidence_id:
                return item
        raise BrowserServiceError(
            "evidence_not_found",
            f"Evidence was not found: {target_evidence_id}",
            404,
        )

    @staticmethod
    def _pointer_tokens(path: str) -> list[str]:
        if path == "/":
            return []
        return [token.replace("~1", "/").replace("~0", "~") for token in path.split("/")[1:]]

    @classmethod
    def _filter_shape_paths(
        cls,
        paths: dict[str, Any],
        *,
        path_prefix: str,
        max_depth: int,
        max_array_items: int,
    ) -> list[tuple[str, Any]]:
        prefix_tokens = cls._pointer_tokens(path_prefix)
        selected: list[tuple[str, Any]] = []
        for path, descriptor in paths.items():
            if not isinstance(path, str):
                continue
            tokens = cls._pointer_tokens(path)
            if tokens[: len(prefix_tokens)] != prefix_tokens:
                continue
            relative = tokens[len(prefix_tokens) :]
            if len(relative) > max_depth:
                continue
            if any(token.isdigit() and int(token) >= max_array_items for token in relative):
                continue
            selected.append((path, descriptor))
        return sorted(selected, key=lambda item: item[0])

    @classmethod
    def _bounded_redacted_subtree(
        cls,
        value: Any,
        *,
        path_prefix: str,
        max_depth: int,
        max_array_items: int,
    ) -> Any:
        current = value
        for token in cls._pointer_tokens(path_prefix):
            if isinstance(current, dict) and token in current:
                current = current[token]
            elif isinstance(current, list) and token.isdigit():
                index = int(token)
                if index >= len(current):
                    return None
                current = current[index]
            else:
                return None

        def prune(item: Any, depth: int) -> Any:
            if depth >= max_depth:
                if isinstance(item, dict):
                    return {"$truncated": "object"}
                if isinstance(item, list):
                    return {"$truncated": "array", "length": len(item)}
                return item
            if isinstance(item, dict):
                keys = sorted(item)[:100]
                result = {key: prune(item[key], depth + 1) for key in keys}
                if len(item) > len(keys):
                    result["$truncated_key_count"] = len(item) - len(keys)
                return result
            if isinstance(item, list):
                result = [prune(child, depth + 1) for child in item[:max_array_items]]
                if len(item) > max_array_items:
                    result.append({"$truncated_array_items": len(item) - max_array_items})
                return result
            return item

        return prune(current, 0)

    def _validate_and_store_series(
        self,
        *,
        session_id: str,
        manifest: dict[str, Any],
        payload: CaptureFlowPayload,
    ) -> None:
        series = payload.series.model_dump(mode="json", exclude_none=True)
        predecessor_id = series.get("predecessor_experiment_id")
        if predecessor_id:
            predecessor = self.experiments.load_manifest(str(predecessor_id))
            if predecessor.get("session_id") != session_id:
                raise BrowserServiceError(
                    "predecessor_session_mismatch",
                    "The predecessor experiment belongs to a different session.",
                    409,
                )
            predecessor_series = predecessor.get("series")
            predecessor_series = predecessor_series if isinstance(predecessor_series, dict) else {}
            requested_series = series.get("analysis_series_id")
            existing_series = predecessor_series.get("analysis_series_id")
            if requested_series and existing_series and requested_series != existing_series:
                raise BrowserServiceError(
                    "predecessor_series_mismatch",
                    "The predecessor experiment belongs to a different analysis series.",
                    409,
                )
        manifest["series"] = series

    @staticmethod
    def _generate_binding_value(binding: VolatileBinding) -> Any:
        if binding.value_source != "generated" or binding.generator is None:
            raise ValueError("Only generated bindings can generate a new value")
        if binding.generator == "uuid4":
            return str(uuid.uuid4())
        if binding.generator == "timestamp_ms":
            return int(time.time() * 1000)
        if binding.generator == "timestamp_iso":
            return datetime.now(UTC).isoformat()
        return secrets.token_hex(16)

    @classmethod
    def _generate_volatile_binding_values(
        cls,
        bindings: list[VolatileBinding],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for binding in bindings:
            if binding.value_source == "generated":
                values[binding.binding_id] = cls._generate_binding_value(binding)
        return values

    @staticmethod
    def _pair_protocol(control: ReplayControlPayload) -> dict[str, Any]:
        return {
            "session_id": control.session_id,
            "source_experiment_id": control.source_experiment_id,
            "source_evidence_id": control.source_evidence_id,
            "mode": control.mode,
            "target": control.target.model_dump(mode="json", exclude_none=True),
            "setup_flow": [
                step.model_dump(mode="json", exclude_none=True) for step in control.setup_flow
            ],
            "setup_outputs": [
                item.model_dump(mode="json", exclude_none=True) for item in control.setup_outputs
            ],
            "wait_for": (
                control.wait_for.model_dump(mode="json", exclude_none=True)
                if control.wait_for
                else None
            ),
            "verification_flow": [
                step.model_dump(mode="json", exclude_none=True)
                for step in control.verification_flow
            ],
            "execution_mode": control.execution_mode,
            "deadline_ms": control.deadline_ms,
            "job_timeout_ms": control.job_timeout_ms,
            "max_response_bytes": control.max_response_bytes,
            "stream_idle_timeout_ms": control.stream_idle_timeout_ms,
            "default_done_marker": control.default_done_marker,
            "default_done_event_name": control.default_done_event_name,
            "response_mode": control.response_mode,
            "terminal_conditions": [
                item.model_dump(mode="json", exclude_none=True)
                for item in control.terminal_conditions
            ],
            "raw_only": control.raw_only,
            "ignored_cookie_names": control.ignored_cookie_names,
            "ignored_context_headers": control.ignored_context_headers,
            "normalize_wire_order": control.normalize_wire_order,
            "environment_comparison": control.environment_comparison.model_dump(mode="json"),
            "transport": control.transport.model_dump(mode="json"),
            "capture": control.capture.model_dump(mode="json"),
            "requirements": control.requirements.model_dump(mode="json"),
            "network_evidence": [
                item.model_dump(mode="json", exclude_none=True) for item in control.network_evidence
            ],
            "volatile_bindings": [
                item.model_dump(mode="json", exclude_none=True)
                for item in control.volatile_bindings
            ],
        }

    def _resolve_replay_pair(
        self,
        payload: ReplayRequestPayload,
    ) -> tuple[
        ReplayControlPayload,
        list[VolatileBinding],
        dict[str, Any],
        dict[str, Any],
        dict[str, Any] | None,
        Any | None,
    ]:
        if isinstance(payload, ReplayControlPayload):
            bindings = list(payload.volatile_bindings)
            values = self._generate_volatile_binding_values(bindings)
            protocol = self._pair_protocol(payload)
            mutations = (
                list(payload.mutations) if isinstance(payload, ReplayExploratoryPayload) else []
            )
            return payload, bindings, values, protocol, None, mutations
        control_id = payload.control_experiment_id
        control = self.experiments.load_manifest(control_id)
        execution_integrity = control.get("execution_integrity")
        evidence_integrity = control.get("evidence_integrity")
        if (
            control.get("status") != "completed"
            or execution_integrity != "complete"
            or evidence_integrity != "complete"
        ):
            raise BrowserServiceError(
                "control_replay_not_usable",
                "Treatment replay requires a completed control with complete execution "
                "and evidence integrity.",
                409,
            )
        control_http_status = control.get("replay_http_status")
        if (
            not isinstance(control_http_status, int)
            or control_http_status < 200
            or control_http_status >= 400
        ):
            raise BrowserServiceError(
                "control_replay_http_failed",
                "Treatment replay requires a control replay with a successful HTTP status.",
                409,
            )
        control_response_classification = control.get("replay_response_classification")
        control_observations = (
            control_response_classification.get("observations")
            if isinstance(control_response_classification, dict)
            else None
        )
        if not isinstance(control_response_classification, dict) or not (
            control_response_classification.get("classification") == "success"
            or isinstance(control_observations, dict)
            and control_observations.get("success_like") is True
        ):
            raise BrowserServiceError(
                "control_replay_response_contract_failed",
                "Treatment replay requires a control with a successful response contract.",
                409,
            )
        replay = control.get("replay")
        replay = replay if isinstance(replay, dict) else {}
        if replay.get("replay_mode") != "control":
            raise BrowserServiceError(
                "control_replay_kind_invalid",
                "control_experiment_id does not reference a control replay.",
                409,
            )
        pair_protocol = replay.get("pair_protocol")
        pair_protocol_hash = replay.get("pair_protocol_hash")
        if not isinstance(pair_protocol, dict) or not isinstance(pair_protocol_hash, str):
            raise BrowserServiceError(
                "control_pair_protocol_missing",
                "Control replay has no immutable pair protocol.",
                409,
            )
        if canonical_json_sha256(pair_protocol) != pair_protocol_hash:
            raise BrowserServiceError(
                "control_pair_protocol_invalid",
                "Control replay pair protocol hash does not match its manifest.",
                409,
            )
        try:
            effective_control = ReplayControlPayload.model_validate(
                {
                    **pair_protocol,
                    "objective": str(control.get("objective") or "paired replay"),
                    "replay_mode": "control",
                    "mutations": [],
                    "series": control.get("series") or {},
                }
            )
        except Exception as exc:
            raise BrowserServiceError(
                "control_pair_protocol_invalid",
                "Control replay pair protocol cannot be reconstructed.",
                409,
            ) from exc
        binding_specs = replay.get("volatile_bindings")
        control_values = replay.get("control_volatile_binding_values")
        if not isinstance(binding_specs, list) or not isinstance(control_values, dict):
            raise BrowserServiceError(
                "control_replay_bindings_missing",
                "Control replay does not contain reusable volatile bindings.",
                409,
            )
        try:
            parsed_specs = [VolatileBinding.model_validate(item) for item in binding_specs]
        except Exception as exc:
            raise BrowserServiceError(
                "control_replay_bindings_invalid",
                "Control replay contains invalid volatile binding metadata.",
                409,
            ) from exc
        treatment_values: dict[str, Any] = {}
        for binding in parsed_specs:
            if binding.value_source == "setup_output":
                continue
            if binding.value_source == "preserve_source" or binding.reuse_policy == "same_value":
                treatment_values[binding.binding_id] = control_values[binding.binding_id]
            else:
                treatment_values[binding.binding_id] = self._generate_binding_value(binding)
        return (
            effective_control,
            parsed_specs,
            treatment_values,
            pair_protocol,
            control,
            payload.mutation,
        )

    async def _all_network_requests(self, deadline: Deadline) -> list[dict[str, Any]]:
        payload = await self.js_reverse.list_network_requests(
            RequestMatcher(),
            deadline,
        )
        requests = payload.get("requests")
        return (
            [item for item in requests if isinstance(item, dict)]
            if isinstance(requests, list)
            else []
        )

    async def _resolve_setup_output_bindings(
        self,
        *,
        setup_outputs: list[Any],
        checkpoint: dict[str, Any],
        experiment_dir: Path,
        deadline: Deadline,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        requests = requests_after_checkpoint(
            await self._all_network_requests(deadline),
            checkpoint,
            include_in_flight=True,
        )
        values: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        output_dir = experiment_dir / "replay" / "setup-outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        for output in setup_outputs:
            matches = [item for item in requests if network_request_matches(item, output.selector)]
            occurrence = output.occurrence
            if occurrence == "first":
                selected = matches[0] if matches else None
            elif occurrence == "last":
                selected = matches[-1] if matches else None
            else:
                selected = matches[occurrence] if occurrence < len(matches) else None
            if not isinstance(selected, dict) or not isinstance(selected.get("reqid"), int):
                raise BrowserServiceError(
                    "setup_output_request_missing",
                    f"No setup request matched output binding {output.binding_id}.",
                    409,
                )
            reqid = int(selected["reqid"])
            snapshot_path = output_dir / f"{output.binding_id}.json"
            await self.js_reverse.export_network_request(
                reqid,
                snapshot_path,
                "all",
                deadline.child(min(5_000, deadline.remaining_ms())),
            )
            snapshot = load_snapshot(snapshot_path)
            response_value = response_value_from_snapshot(snapshot)
            if response_value is None:
                raise BrowserServiceError(
                    "setup_output_response_unavailable",
                    f"Setup output {output.binding_id} has no complete response body.",
                    409,
                )
            try:
                value = json_pointer_value(response_value, output.pointer)
            except ValueError as exc:
                raise BrowserServiceError(
                    "setup_output_pointer_missing",
                    f"Setup output {output.binding_id}: {exc}",
                    409,
                ) from exc
            values[output.binding_id] = value
            records.append(
                {
                    "binding_id": output.binding_id,
                    "source": output.source,
                    "pointer": output.pointer,
                    "request_id": reqid,
                    "request_url_sha256": canonical_json_sha256(
                        str(selected.get("url") or "")
                    ),
                    "value_sha256": canonical_json_sha256(value),
                    "snapshot_relative_path": self.experiments.relative_path(snapshot_path),
                }
            )
        return values, records

    async def _export_network_evidence(
        self,
        *,
        experiment_id: str,
        experiment_dir: Path,
        selectors: list[Any],
        requests: list[dict[str, Any]],
        deadline: Deadline,
        step_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        evidence_entries: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        warnings: list[str] = []
        network_root = experiment_dir / "js-reverse" / "network"
        network_root.mkdir(parents=True, exist_ok=True)
        for selector in selectors:
            matches = select_network_evidence(requests, selector)
            for ordinal, request in enumerate(matches, start=1):
                reqid = request.get("reqid")
                if not isinstance(reqid, int):
                    continue
                ev_id = evidence_id(
                    experiment_id,
                    "network_request",
                    selector_id=selector.selector_id,
                    stable_id=reqid,
                    ordinal=ordinal,
                )
                target_dir = network_root / ev_id
                target_dir.mkdir(parents=True, exist_ok=True)
                artifact_ids: list[str] = []
                artifact_paths: dict[str, str] = {}
                snapshot: dict[str, Any] | None = None
                for part in list(dict.fromkeys(selector.export_parts)):
                    suffix = ".bin" if part in {"requestBody", "responseBody"} else ".json"
                    output_file = target_dir / f"{part}{suffix}"
                    try:
                        await self.js_reverse.export_network_request(
                            reqid,
                            output_file,
                            part,
                            deadline.child(min(5_000, deadline.remaining_ms())),
                        )
                    except Exception as exc:
                        warnings.append(
                            f"network evidence {selector.selector_id} reqid={reqid} "
                            f"part={part}: {str(exc)[:2000]}"
                        )
                        continue
                    artifact_id = f"art_{ev_id}_{part}"
                    descriptor = self.experiments.describe_local_artifact(
                        str(output_file),
                        artifact_id=artifact_id,
                        kind=f"network_{part}",
                        sensitivity=(
                            "credential"
                            if part
                            in {
                                "all",
                                "requestBody",
                                "responseBody",
                                "responseHeaders",
                            }
                            else "private"
                        ),
                        contains_credentials=part
                        in {
                            "all",
                            "requestBody",
                            "responseBody",
                            "responseHeaders",
                        },
                    )
                    if descriptor:
                        artifacts.append(descriptor)
                        artifact_ids.append(artifact_id)
                        artifact_paths[part] = str(descriptor["relativePath"])
                    if part == "all" and output_file.is_file():
                        try:
                            snapshot = load_snapshot(output_file)
                        except (OSError, ValueError, json.JSONDecodeError) as exc:
                            warnings.append(
                                f"network evidence snapshot reqid={reqid}: {str(exc)[:1000]}"
                            )
                if snapshot is not None:
                    shape = request_shape_from_snapshot(snapshot)
                    redacted_body = redacted_request_body_from_snapshot(snapshot)
                    if shape is not None:
                        shape_file = target_dir / "request-shape.json"
                        shape_file.write_text(
                            json.dumps(shape, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        shape_artifact_id = f"art_{ev_id}_request_shape"
                        descriptor = self.experiments.describe_local_artifact(
                            str(shape_file),
                            artifact_id=shape_artifact_id,
                            kind="request_shape",
                            sensitivity="public",
                        )
                        if descriptor:
                            artifacts.append(descriptor)
                            artifact_ids.append(shape_artifact_id)
                            artifact_paths["request_shape"] = str(descriptor["relativePath"])
                    if redacted_body is not None:
                        redacted_file = target_dir / "request-body.redacted.json"
                        redacted_file.write_text(
                            json.dumps(redacted_body, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        redacted_artifact_id = f"art_{ev_id}_request_body_redacted"
                        descriptor = self.experiments.describe_local_artifact(
                            str(redacted_file),
                            artifact_id=redacted_artifact_id,
                            kind="request_body_redacted",
                            sensitivity="public",
                        )
                        if descriptor:
                            artifacts.append(descriptor)
                            artifact_ids.append(redacted_artifact_id)
                            artifact_paths["request_body_redacted"] = str(
                                descriptor["relativePath"]
                            )
                if selector.include_initiator:
                    try:
                        initiator = await self.js_reverse.get_request_initiator(
                            reqid,
                            deadline.child(min(3_000, deadline.remaining_ms())),
                        )
                        initiator_file = target_dir / "initiator.json"
                        initiator_file.write_text(
                            json.dumps(initiator, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        initiator_artifact_id = f"art_{ev_id}_initiator"
                        descriptor = self.experiments.describe_local_artifact(
                            str(initiator_file),
                            artifact_id=initiator_artifact_id,
                            kind="request_initiator",
                            sensitivity="private",
                        )
                        if descriptor:
                            artifacts.append(descriptor)
                            artifact_ids.append(initiator_artifact_id)
                            artifact_paths["initiator"] = str(descriptor["relativePath"])
                    except Exception as exc:
                        warnings.append(f"request initiator reqid={reqid}: {str(exc)[:2000]}")
                cookie_artifacts: list[str] = []
                if selector.include_cookie_provenance:
                    for cookie_name in selector.cookie_names:
                        try:
                            cookie_flow = await self.js_reverse.trace_cookie_provenance(
                                cookie_name,
                                deadline.child(min(3_000, deadline.remaining_ms())),
                            )
                            safe_cookie = re.sub(r"[^A-Za-z0-9_.-]+", "-", cookie_name)
                            cookie_file = target_dir / f"cookie-{safe_cookie}.json"
                            cookie_file.write_text(
                                json.dumps(cookie_flow, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8",
                            )
                            cookie_artifact_id = f"art_{ev_id}_cookie_{safe_cookie}"
                            descriptor = self.experiments.describe_local_artifact(
                                str(cookie_file),
                                artifact_id=cookie_artifact_id,
                                kind="cookie_provenance",
                                sensitivity="credential",
                                contains_credentials=True,
                            )
                            if descriptor:
                                artifacts.append(descriptor)
                                artifact_ids.append(cookie_artifact_id)
                                cookie_artifacts.append(cookie_artifact_id)
                        except Exception as exc:
                            warnings.append(f"cookie provenance {cookie_name}: {str(exc)[:2000]}")
                evidence_entries.append(
                    {
                        "evidence_id": ev_id,
                        "kind": "network_request",
                        "selector_id": selector.selector_id,
                        "request_ids": {
                            "reqid": reqid,
                            "collector_generation": (
                                snapshot.get("collectorGeneration")
                                if isinstance(snapshot, dict)
                                and snapshot.get("collectorGeneration") is not None
                                else request.get("collectorGeneration")
                                if request.get("collectorGeneration") is not None
                                else self._transport_generation()
                            ),
                            "network_request_id": (
                                snapshot.get("networkRequestId")
                                if isinstance(snapshot, dict)
                                else request.get("networkRequestId")
                            ),
                            "cdp_request_id": (
                                snapshot.get("cdpRequestId")
                                if isinstance(snapshot, dict)
                                else request.get("cdpRequestId")
                            ),
                            "persistent_request_id": (
                                snapshot.get("persistentRequestId")
                                if isinstance(snapshot, dict)
                                else request.get("persistentRequestId")
                            ),
                        },
                        "request_body_canonical_sha256": (
                            request_body_canonical_sha256_from_snapshot(snapshot)
                            if isinstance(snapshot, dict)
                            else None
                        ),
                        "observed_at": (
                            snapshot.get("observedAt")
                            if isinstance(snapshot, dict)
                            else request.get("observedAt")
                        ),
                        "artifact_ids": artifact_ids,
                        "artifact_paths": artifact_paths,
                        "cookie_artifact_ids": cookie_artifacts,
                        "step_ids": step_ids,
                        "summary": (
                            public_network_summary(snapshot)
                            if snapshot is not None
                            else {
                                "url": str(request.get("url", ""))[:8192],
                                "method": request.get("method"),
                                "resource_type": request.get("resourceType"),
                                "status": request.get("status"),
                            }
                        ),
                    }
                )
        return evidence_entries, artifacts, warnings

    async def _console_checkpoint(self, deadline: Deadline) -> dict[str, Any]:
        payload = await self.js_reverse.list_console_messages(
            deadline,
            types=["error", "warn"],
        )
        messages = payload.get("messages")
        values = (
            [item for item in messages if isinstance(item, dict)]
            if isinstance(messages, list)
            else []
        )
        ids = [int(item["msgid"]) for item in values if isinstance(item.get("msgid"), int)]
        return {"max_msgid": max(ids, default=0)}

    async def _export_console_evidence(
        self,
        *,
        experiment_id: str,
        experiment_dir: Path,
        checkpoint: dict[str, Any],
        deadline: Deadline,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        try:
            payload = await self.js_reverse.list_console_messages(
                deadline,
                types=["error", "warn"],
            )
        except Exception as exc:
            return [], [], [f"console evidence: {str(exc)[:2000]}"]
        messages = payload.get("messages")
        values = (
            [item for item in messages if isinstance(item, dict)]
            if isinstance(messages, list)
            else []
        )
        max_msgid = int(checkpoint.get("max_msgid", 0) or 0)
        selected = [
            item
            for item in values
            if isinstance(item.get("msgid"), int) and int(item["msgid"]) > max_msgid
        ]
        if not selected:
            return [], [], []
        console_dir = experiment_dir / "js-reverse" / "console"
        console_dir.mkdir(parents=True, exist_ok=True)
        console_file = console_dir / "console.jsonl"
        console_file.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in selected),
            encoding="utf-8",
        )
        artifact_id = f"art_{experiment_id}_console_errors"
        descriptor = self.experiments.describe_local_artifact(
            str(console_file),
            artifact_id=artifact_id,
            kind="console_errors",
            sensitivity="private",
        )
        artifacts = [descriptor] if descriptor else []
        evidence_entries = [
            {
                "evidence_id": evidence_id(
                    experiment_id,
                    "console_message",
                    stable_id=item.get("msgid"),
                    ordinal=index,
                ),
                "kind": "console_message",
                "message_id": item.get("msgid"),
                "artifact_ids": [artifact_id] if descriptor else [],
                "artifact_paths": {"console": descriptor["relativePath"] if descriptor else None},
                "message_index": index - 1,
                "summary": {
                    key: item.get(key)
                    for key in (
                        "type",
                        "text",
                        "url",
                        "lineNumber",
                        "columnNumber",
                        "timestamp",
                    )
                    if key in item
                },
            }
            for index, item in enumerate(selected, start=1)
        ]
        return evidence_entries, artifacts, []

    @staticmethod
    def _network_evidence_snapshot(
        root: Path,
        entry: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        paths = entry.get("artifact_paths")
        paths = paths if isinstance(paths, dict) else {}
        relative = paths.get("all")
        if not isinstance(relative, str):
            return None
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
            return load_snapshot(path)
        except (ValueError, OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _select_replay_network_evidence(
        entries: list[dict[str, Any]],
        replay_plan: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        expected_hash = replay_plan.get("expected_request_body_canonical_sha256")
        expected_spec = replay_plan.get("spec")
        expected_spec = expected_spec if isinstance(expected_spec, dict) else {}
        expected_url = str(expected_spec.get("url") or "")
        expected_method = str(expected_spec.get("method") or "GET").upper()
        dispatch_wall_time_ms = replay_plan.get("dispatch_wall_time_ms")
        window_end_wall_time_ms = replay_plan.get("correlation_window_end_wall_time_ms")
        if not isinstance(dispatch_wall_time_ms, int) or not isinstance(
            window_end_wall_time_ms, int
        ):
            return None, "Replay correlation window is missing."
        candidates = [
            item
            for item in entries
            if item.get("kind") == "network_request"
            and item.get("selector_id") == "replay_request"
            and isinstance(item.get("summary"), dict)
            and str(item["summary"].get("url") or "") == expected_url
            and str(item["summary"].get("method") or "").upper() == expected_method
            and (
                expected_hash is None or item.get("request_body_canonical_sha256") == expected_hash
            )
            and (
                isinstance(item.get("observed_at"), (int, float))
                and dispatch_wall_time_ms - 1_000
                <= int(item["observed_at"])
                <= window_end_wall_time_ms
            )
        ]
        if len(candidates) == 1:
            return candidates[0], None
        if not candidates:
            return (
                None,
                "No replay request matched the expected method, full URL, canonical body "
                "fingerprint, and dispatch window.",
            )
        return (
            None,
            "Multiple replay requests matched the method, full URL, canonical body "
            "fingerprint, and dispatch window; the replay request is ambiguous.",
        )

    @staticmethod
    def _associate_stream_network_evidence(
        request: dict[str, Any],
        network_entries: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        def request_ids(item: dict[str, Any]) -> dict[str, Any]:
            value = item.get("request_ids")
            return value if isinstance(value, dict) else {}

        tiers: list[tuple[str, list[dict[str, Any]]]] = []
        network_request_id = request.get("networkRequestId")
        collector_generation = request.get("collectorGeneration")
        if network_request_id is not None:
            tiers.append(
                (
                    "network_request_id_and_generation",
                    [
                        item
                        for item in network_entries
                        if request_ids(item).get("network_request_id") == network_request_id
                        and (
                            collector_generation is None
                            or request_ids(item).get("collector_generation") == collector_generation
                        )
                    ],
                )
            )
        cdp_request_id = request.get("cdpRequestId")
        if cdp_request_id is not None:
            tiers.append(
                (
                    "cdp_request_id",
                    [
                        item
                        for item in network_entries
                        if request_ids(item).get("cdp_request_id") == cdp_request_id
                    ],
                )
            )
        persistent_request_id = request.get("persistentRequestId")
        if persistent_request_id is not None:
            tiers.append(
                (
                    "persistent_request_id",
                    [
                        item
                        for item in network_entries
                        if request_ids(item).get("persistent_request_id") == persistent_request_id
                    ],
                )
            )
        tiers.append(
            (
                "url_method_fallback",
                [
                    item
                    for item in network_entries
                    if isinstance(item.get("summary"), dict)
                    and item["summary"].get("url") == request.get("url")
                    and item["summary"].get("method") == request.get("method")
                ],
            )
        )
        for tier, candidates in tiers:
            if len(candidates) == 1:
                return candidates[0], {
                    "status": "matched",
                    "method": tier,
                    "candidate_count": 1,
                }
            if len(candidates) > 1:
                return None, {
                    "status": "ambiguous",
                    "method": tier,
                    "candidate_count": len(candidates),
                }
        return None, {
            "status": "not_found",
            "method": None,
            "candidate_count": 0,
        }

    @staticmethod
    def _stream_request_has_complete_request_headers(
        request: dict[str, Any],
    ) -> bool:
        artifacts = request.get("coreArtifacts")
        if not isinstance(artifacts, list):
            return False
        by_kind = {
            str(item.get("kind")): item
            for item in artifacts
            if isinstance(item, dict) and item.get("kind")
        }
        request_headers = by_kind.get("request_headers")
        request_headers_extra = by_kind.get("request_headers_extra")
        if not isinstance(request_headers, dict) or not isinstance(
            request_headers_extra,
            dict,
        ):
            return False
        for descriptor in (request_headers, request_headers_extra):
            if descriptor.get("writeStatus") not in {None, "written"}:
                return False
            if isinstance(descriptor.get("bytes"), int) and descriptor["bytes"] <= 0:
                return False
        return True

    @classmethod
    def _mark_snapshot_headers_complete_from_stream(
        cls,
        snapshot: dict[str, Any] | None,
        stream_request: dict[str, Any] | None,
    ) -> None:
        if not isinstance(snapshot, dict) or not isinstance(stream_request, dict):
            return
        if cls._stream_request_has_complete_request_headers(stream_request):
            snapshot["requestHeadersCompleteness"] = "complete"

    @staticmethod
    def _augment_network_request_with_evidence(
        request: dict[str, Any],
        evidence_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        reqid = request.get("reqid")
        candidates = [
            item
            for item in evidence_entries
            if item.get("kind") == "network_request"
            and isinstance(item.get("request_ids"), dict)
            and item["request_ids"].get("reqid") == reqid
        ]
        if len(candidates) != 1:
            return {
                **request,
                "networkEvidenceAssociation": {
                    "status": "ambiguous" if candidates else "not_found",
                    "candidate_count": len(candidates),
                },
            }
        evidence = candidates[0]
        summary = evidence.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        integrity = summary.get("snapshot_integrity")
        integrity = integrity if isinstance(integrity, dict) else {}
        network_snapshot_integrity = str(integrity.get("network_snapshot_integrity") or "partial")
        network_artifact_integrity = (
            "complete"
            if isinstance(evidence.get("artifact_paths"), dict)
            and evidence["artifact_paths"].get("all")
            else "partial"
        )
        return {
            **request,
            "networkEvidenceId": evidence.get("evidence_id"),
            "networkEvidenceAssociation": {
                "status": "matched",
                "method": "reqid",
                "candidate_count": 1,
            },
            "networkSnapshotIntegrity": network_snapshot_integrity,
            "requestBodyCompleteness": integrity.get("request_body_completeness"),
            "requestHeadersCompleteness": integrity.get("request_headers_completeness"),
            "responseBodyCompleteness": integrity.get("response_body_completeness"),
            "responseHeadersCompleteness": integrity.get("response_headers_completeness"),
            "networkArtifactIntegrity": network_artifact_integrity,
            "artifactIntegrity": network_artifact_integrity,
            "integrityStatus": max(
                (network_snapshot_integrity, network_artifact_integrity),
                key=BrowserActionService._integrity_severity,
            ),
        }

    @classmethod
    def _extract_http_status(cls, value: Any) -> int | None:
        if isinstance(value, dict):
            status = value.get("status")
            if isinstance(status, int):
                return status
            for child in value.values():
                found = cls._extract_http_status(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._extract_http_status(child)
                if found is not None:
                    return found
        elif isinstance(value, str):
            try:
                return cls._extract_http_status(json.loads(value))
            except json.JSONDecodeError:
                return None
        return None

    @classmethod
    def _extract_response_content_type(cls, value: Any) -> str | None:
        if isinstance(value, dict):
            headers = value.get("headers")
            if isinstance(headers, list):
                for item in headers:
                    if (
                        isinstance(item, list)
                        and len(item) >= 2
                        and str(item[0]).lower() == "content-type"
                    ):
                        return str(item[1]).split(";", 1)[0].strip().lower()
                    if (
                        isinstance(item, dict)
                        and str(item.get("name", "")).lower() == "content-type"
                    ):
                        return str(item.get("value", "")).split(";", 1)[0].strip().lower()
            for child in value.values():
                found = cls._extract_response_content_type(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._extract_response_content_type(child)
                if found:
                    return found
        elif isinstance(value, str):
            try:
                return cls._extract_response_content_type(json.loads(value))
            except json.JSONDecodeError:
                return None
        return None

    @classmethod
    def _extract_response_field(cls, value: Any, field: str) -> Any:
        if isinstance(value, dict):
            if field in value:
                return value[field]
            for child in value.values():
                found = cls._extract_response_field(child, field)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._extract_response_field(child, field)
                if found is not None:
                    return found
        elif isinstance(value, str):
            try:
                return cls._extract_response_field(json.loads(value), field)
            except json.JSONDecodeError:
                return None
        return None

    @classmethod
    def _complete_replay_response_value(cls, value: Any) -> Any | None:
        preview = cls._extract_response_field(value, "bodyPreview")
        byte_length = cls._extract_response_field(value, "bodyByteLength")
        truncated = bool(cls._extract_response_field(value, "truncated"))
        termination = cls._extract_response_field(value, "terminationReason")
        if (
            not isinstance(preview, str)
            or not isinstance(byte_length, int)
            or truncated
            or termination not in {"network_close", "no_response_body"}
            or len(preview.encode("utf-8")) != byte_length
        ):
            return None
        try:
            return json.loads(preview)
        except json.JSONDecodeError:
            return preview

    @classmethod
    def _stream_response_contract(
        cls,
        replay_plan: dict[str, Any],
        replay_response: Any,
        *,
        status: int | None,
        content_type: str | None,
    ) -> dict[str, Any] | None:
        if replay_plan.get("source_is_stream") is not True:
            return None
        response_control = replay_plan.get("spec", {}).get("responseControl", {})
        response_control = response_control if isinstance(response_control, dict) else {}
        response_mode = str(response_control.get("responseMode") or "auto")
        observed_mode = cls._extract_response_field(replay_response, "responseMode")
        terminal_conditions = response_control.get("terminalConditions")
        terminal_conditions = (
            [item for item in terminal_conditions if isinstance(item, dict)]
            if isinstance(terminal_conditions, list)
            else []
        )
        marker = response_control.get("doneMarker")
        event_name = response_control.get("doneEventName")
        termination = cls._extract_response_field(replay_response, "terminationReason")
        terminal_condition_matched = cls._extract_response_field(
            replay_response,
            "terminalConditionMatched",
        )
        marker_observed = bool(cls._extract_response_field(replay_response, "doneMarkerObserved"))
        observed_event_name = cls._extract_response_field(
            replay_response,
            "doneEventNameObserved",
        )
        truncated = bool(cls._extract_response_field(replay_response, "truncated"))
        if isinstance(status, int) and status >= 400 and content_type != "text/event-stream":
            contract_status = "not_applicable_non_stream_response"
        else:
            marker_ok = not marker or marker_observed
            event_ok = not event_name or observed_event_name == event_name
            terminal_types = {
                str(item.get("type")) for item in terminal_conditions if item.get("type")
            } or {"network_close"}
            terminal_ok = bool(
                ("exact_sse_data" in terminal_types and termination == "done_marker")
                or ("byte_pattern" in terminal_types and termination == "byte_pattern")
                or (
                    "network_close" in terminal_types
                    and termination in {"network_close", "no_response_body"}
                )
                or ("idle_window" in terminal_types and termination == "idle_timeout")
            )
            mode_ok = bool(
                response_mode == "raw_stream"
                or response_mode == "auto"
                or response_mode == "sse"
                and content_type == "text/event-stream"
                or response_mode == "ndjson"
                and content_type
                in {"application/x-ndjson", "application/ndjson", "application/json-seq"}
            )
            complete = bool(
                isinstance(status, int)
                and 200 <= status < 400
                and mode_ok
                and marker_ok
                and event_ok
                and terminal_ok
                and not truncated
            )
            contract_status = "complete" if complete else "partial"
        return {
            "status": contract_status,
            "response_mode": response_mode,
            "observed_response_mode": observed_mode,
            "terminal_conditions": terminal_conditions,
            "terminal_condition_matched": terminal_condition_matched,
            "observed_termination": termination,
            "done_marker_required": bool(marker),
            "done_marker_observed": marker_observed,
            "done_event_name_required": event_name,
            "done_event_name_observed": observed_event_name,
            "truncated": truncated,
        }

    @staticmethod
    def _sha256_lines(values: list[str]) -> str:
        return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()

    @classmethod
    def _request_context_hashes(
        cls,
        snapshot: dict[str, Any] | None,
        *,
        ignored_cookie_names: list[str] | None = None,
        ignored_context_headers: list[str] | None = None,
        context_header_names: list[str] | None = None,
    ) -> dict[str, Any]:
        headers = snapshot.get("requestHeadersArray") if isinstance(snapshot, dict) else None
        dimensions = network_snapshot_dimensions(snapshot) if isinstance(snapshot, dict) else {}
        if (
            not isinstance(headers, list)
            or dimensions.get("request_headers_completeness") != "complete"
        ):
            return {
                "status": "unavailable",
                "cookie_name_value_sha256": None,
                "authorization_sha256": None,
                "csrf_header_sha256": None,
                "context_headers_sha256": None,
                "request_context_sha256": None,
            }
        ignored_cookies = {
            item.strip().lower() for item in (ignored_cookie_names or []) if item.strip()
        }
        ignored_headers = {
            item.strip().lower() for item in (ignored_context_headers or []) if item.strip()
        }
        selected_context_headers = {
            item.strip().lower()
            for item in (
                context_header_names
                or [
                    "authorization",
                    "proxy-authorization",
                    "x-csrf-token",
                    "x-xsrf-token",
                ]
            )
            if item.strip()
        }
        cookies: list[str] = []
        authorization: list[str] = []
        csrf: list[str] = []
        context_headers: list[str] = []
        for header_index, item in enumerate(headers):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().lower()
            value = str(item.get("value", ""))
            if name in ignored_headers:
                continue
            if name == "cookie":
                for segment_index, segment in enumerate(value.split(";")):
                    normalized_segment = segment.strip()
                    if not normalized_segment:
                        continue
                    cookie_name = normalized_segment.split("=", 1)[0].strip().lower()
                    if cookie_name in ignored_cookies:
                        continue
                    cookies.append(f"{header_index}:{segment_index}:{normalized_segment}")
            elif name in {"authorization", "proxy-authorization"}:
                authorization.append(f"{header_index}:{name}:{value}")
            elif "csrf" in name or "xsrf" in name:
                csrf.append(f"{header_index}:{name}:{value}")
            if name in selected_context_headers:
                context_headers.append(f"{header_index}:{name}:{value}")
        cookie_hash = cls._sha256_lines(cookies)
        authorization_hash = cls._sha256_lines(authorization)
        csrf_hash = cls._sha256_lines(csrf)
        context_headers_hash = cls._sha256_lines(context_headers)
        return {
            "status": "observed",
            "cookie_name_value_sha256": cookie_hash,
            "authorization_sha256": authorization_hash,
            "csrf_header_sha256": csrf_hash,
            "context_headers_sha256": context_headers_hash,
            "request_context_sha256": cls._sha256_lines(
                [
                    f"cookie:{cookie_hash}",
                    f"context_headers:{context_headers_hash}",
                ]
            ),
            "ignored_cookie_names": sorted(ignored_cookies),
            "ignored_context_headers": sorted(ignored_headers),
            "context_header_names": sorted(selected_context_headers),
        }

    @classmethod
    def _environment_fingerprint(
        cls,
        alignment: AlignmentResult | None,
        wire_snapshot: dict[str, Any] | None,
        *,
        phase: str,
        include_request_context: bool = True,
        ignored_cookie_names: list[str] | None = None,
        ignored_context_headers: list[str] | None = None,
        context_header_names: list[str] | None = None,
    ) -> dict[str, Any]:
        page_id = alignment.js_reverse_page_id if alignment is not None else None
        page_url = (
            alignment.js_reverse_page_url or alignment.playwright_page.url
            if alignment is not None
            else None
        )
        page_split = urlsplit(page_url or "")
        request_url = str(wire_snapshot.get("url", "")) if isinstance(wire_snapshot, dict) else ""
        request_split = urlsplit(request_url)
        request_context = (
            cls._request_context_hashes(
                wire_snapshot,
                ignored_cookie_names=ignored_cookie_names,
                ignored_context_headers=ignored_context_headers,
                context_header_names=context_header_names,
            )
            if include_request_context
            else {
                "status": "not_applicable",
                "cookie_name_value_sha256": None,
                "authorization_sha256": None,
                "csrf_header_sha256": None,
                "context_headers_sha256": None,
                "request_context_sha256": None,
                "ignored_cookie_names": sorted(ignored_cookie_names or []),
                "ignored_context_headers": sorted(ignored_context_headers or []),
                "context_header_names": sorted(context_header_names or []),
            }
        )
        unavailable = [
            "conversation_current_node",
            "critical_bundle_sha256",
        ]
        if alignment is None or alignment.status != "aligned":
            unavailable.extend(["page_id", "page_url", "page_origin"])
        if include_request_context and request_context["status"] != "observed":
            unavailable.append("request_context_sha256")
        return {
            "phase": phase,
            "page_id": page_id,
            "page_url": page_url,
            "page_origin": (
                f"{page_split.scheme}://{page_split.netloc}"
                if page_split.scheme and page_split.netloc
                else None
            ),
            "request_origin": (
                f"{request_split.scheme}://{request_split.netloc}"
                if request_split.scheme and request_split.netloc
                else None
            ),
            "request_path": request_split.path or None,
            **request_context,
            "unavailable_dimensions": sorted(set(unavailable)),
        }

    @staticmethod
    def _compare_pair_environments(
        control: dict[str, Any] | None,
        treatment: dict[str, Any],
        *,
        required_dimensions: list[str] | None = None,
        advisory_dimensions: list[str] | None = None,
    ) -> dict[str, Any]:
        required_keys = list(
            dict.fromkeys(required_dimensions or ["page_origin", "request_context_sha256"])
        )
        advisory_keys = [
            item
            for item in dict.fromkeys(
                advisory_dimensions
                or [
                    "page_url",
                    "request_origin",
                    "request_path",
                    "conversation_current_node",
                    "critical_bundle_sha256",
                ]
            )
            if item not in required_keys
        ]
        all_keys = [*required_keys, *advisory_keys]
        if not isinstance(control, dict):
            return {
                "status": "insufficient",
                "equivalent": False,
                "differences": ["control_environment_fingerprint_missing"],
                "compared_dimensions": [],
                "unavailable_dimensions": all_keys,
                "required_dimensions_missing": required_keys,
                "advisory_dimensions_missing": advisory_keys,
                "required_dimensions": required_keys,
                "advisory_dimensions": advisory_keys,
            }
        compared = [
            key
            for key in all_keys
            if control.get(key) is not None and treatment.get(key) is not None
        ]
        required_missing = [key for key in required_keys if key not in compared]
        advisory_missing = [key for key in advisory_keys if key not in compared]
        unavailable = sorted(
            set(control.get("unavailable_dimensions") or [])
            | set(treatment.get("unavailable_dimensions") or [])
        )
        required_missing = sorted(set(required_missing) | (set(unavailable) & set(required_keys)))
        advisory_missing = sorted(set(advisory_missing) | (set(unavailable) & set(advisory_keys)))
        differences = [key for key in compared if control.get(key) != treatment.get(key)]
        required_differences = [key for key in differences if key in required_keys]
        advisory_differences = [key for key in differences if key in advisory_keys]
        status = (
            "different"
            if required_differences
            else "insufficient"
            if required_missing
            else "observed_equivalent"
        )
        return {
            "status": status,
            "equivalent": status == "observed_equivalent",
            "observed_dimensions_equivalent": not required_differences,
            "differences": differences,
            "required_differences": required_differences,
            "advisory_differences": advisory_differences,
            "compared_dimensions": compared,
            "unavailable_dimensions": unavailable,
            "required_dimensions_missing": required_missing,
            "advisory_dimensions_missing": advisory_missing,
            "required_dimensions": required_keys,
            "advisory_dimensions": advisory_keys,
        }

    @staticmethod
    def _stream_evidence_entries(
        experiment_id: str,
        primary_requests: list[dict[str, Any]],
        network_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for ordinal, request in enumerate(primary_requests, start=1):
            persistent_id = request.get("persistentRequestId")
            cdp_id = request.get("cdpRequestId")
            core_artifacts = request.get("coreArtifacts")
            core_artifacts = (
                [item for item in core_artifacts if isinstance(item, dict)]
                if isinstance(core_artifacts, list)
                else []
            )
            artifact_ids = [
                str(item.get("artifactId")) for item in core_artifacts if item.get("artifactId")
            ]
            artifact_paths = {
                str(item.get("kind") or item.get("artifactId")): str(item.get("relativePath"))
                for item in core_artifacts
                if item.get("relativePath")
            }
            linked_network, association = BrowserActionService._associate_stream_network_evidence(
                request,
                network_entries,
            )
            linked_network_id = (
                linked_network.get("evidence_id") if isinstance(linked_network, dict) else None
            )
            linked_summary = (
                linked_network.get("summary")
                if isinstance(linked_network, dict)
                and isinstance(linked_network.get("summary"), dict)
                else {}
            )
            snapshot_integrity = linked_summary.get("snapshot_integrity")
            snapshot_integrity = snapshot_integrity if isinstance(snapshot_integrity, dict) else {}
            stream_id = evidence_id(
                experiment_id,
                "stream_request",
                stable_id=persistent_id or cdp_id or ordinal,
            )
            entries.append(
                {
                    "evidence_id": stream_id,
                    "kind": "stream_request",
                    "request_ids": {
                        "persistent": persistent_id,
                        "cdp": cdp_id,
                        "network": request.get("networkRequestId"),
                        "collector_generation": request.get("collectorGeneration"),
                        "network_evidence_id": linked_network_id,
                    },
                    "artifact_ids": artifact_ids,
                    "artifact_paths": artifact_paths,
                    "summary": {
                        "url": str(request.get("url", ""))[:8192],
                        "method": request.get("method"),
                        "status": request.get("status"),
                        "terminal_reason": request.get("terminalReason"),
                        "primary_event_source": request.get("primaryEventSource"),
                        "raw_event_count": request.get("rawEventCount"),
                        "semantic_event_count": request.get("semanticEventCount"),
                        "raw_capture_integrity": request.get("rawCaptureIntegrity"),
                        "semantic_parse_integrity": request.get("semanticParseIntegrity"),
                        "request_snapshot_integrity": request.get("requestSnapshotIntegrity"),
                        "stream_artifact_integrity": request.get("artifactIntegrity"),
                        "network_snapshot_integrity": snapshot_integrity.get(
                            "network_snapshot_integrity"
                        ),
                        "request_body_completeness": snapshot_integrity.get(
                            "request_body_completeness"
                        ),
                        "request_headers_completeness": snapshot_integrity.get(
                            "request_headers_completeness"
                        ),
                        "network_evidence_association": association,
                    },
                }
            )
            ranges = [
                (
                    "raw-stream",
                    int(request.get("rawEventCount") or 0),
                    {"events", "decoded_sse", "chunks"},
                ),
                (
                    "eventsource",
                    int(request.get("semanticEventCount") or 0),
                    {"eventsource_events"},
                ),
            ]
            for event_source, event_count, artifact_kinds in ranges:
                event_artifacts = [
                    str(item.get("artifactId"))
                    for item in core_artifacts
                    if str(item.get("kind", "")) in artifact_kinds and item.get("artifactId")
                ]
                if event_count <= 0 or not event_artifacts:
                    continue
                entries.append(
                    {
                        "evidence_id": evidence_id(
                            experiment_id,
                            "stream_event_range",
                            selector_id=event_source,
                            stable_id=persistent_id or cdp_id or ordinal,
                        ),
                        "kind": "stream_event_range",
                        "stream_request_evidence_id": stream_id,
                        "event_source": event_source,
                        "start_event_index": 0,
                        "end_event_index": event_count - 1,
                        "artifact_ids": event_artifacts,
                    }
                )
        return entries

    def _prepare_replay_execution(
        self,
        request: ReplayRequestRequest,
    ) -> tuple[CaptureFlowPayload, dict[str, Any]]:
        request_payload = request.payload
        (
            control_payload,
            binding_specs,
            current_binding_values,
            pair_protocol,
            control_manifest,
            mutation_payload,
        ) = self._resolve_replay_pair(request_payload)
        mutations = (
            list(mutation_payload)
            if isinstance(mutation_payload, list)
            else [mutation_payload]
            if mutation_payload is not None
            else []
        )
        mutation = mutations[0] if len(mutations) == 1 else None
        replay_mode = request_payload.replay_mode
        pair_protocol_hash = canonical_json_sha256(pair_protocol)
        control_values = (
            dict((control_manifest.get("replay") or {}).get("control_volatile_binding_values", {}))
            if isinstance(control_manifest, dict)
            else dict(current_binding_values)
        )
        source_manifest = self.experiments.load_manifest(control_payload.source_experiment_id)
        if source_manifest.get("session_id") != control_payload.session_id:
            raise BrowserServiceError(
                "source_experiment_session_mismatch",
                "Replay source evidence belongs to a different browser session.",
                409,
            )
        source_evidence = self._find_evidence(
            source_manifest,
            control_payload.source_evidence_id,
        )
        if source_evidence.get("kind") != "network_request":
            raise BrowserServiceError(
                "replay_source_kind_invalid",
                "replay_request requires network_request evidence.",
                409,
            )
        artifact_paths = source_evidence.get("artifact_paths")
        artifact_paths = artifact_paths if isinstance(artifact_paths, dict) else {}
        snapshot_relative = artifact_paths.get("all")
        if not isinstance(snapshot_relative, str):
            raise BrowserServiceError(
                "replay_source_snapshot_missing",
                "The source evidence has no exact full network snapshot artifact.",
                409,
            )
        snapshot_path = (self.experiments.root / snapshot_relative).resolve()
        try:
            snapshot_path.relative_to(self.experiments.root)
        except ValueError as exc:
            raise BrowserServiceError(
                "replay_source_path_invalid",
                "The source snapshot is outside the analysis workspace.",
                409,
            ) from exc
        if not snapshot_path.is_file():
            raise BrowserServiceError(
                "replay_source_snapshot_missing",
                "The source network snapshot file is missing.",
                404,
            )
        try:
            snapshot = load_snapshot(snapshot_path)
            for requested_mutation in mutations:
                validate_binding_mutation_compatibility(
                    binding_specs,
                    requested_mutation,
                )
            if not isinstance(control_manifest, dict):
                for binding in binding_specs:
                    if binding.value_source == "preserve_source":
                        current_binding_values[binding.binding_id] = binding_value_from_snapshot(
                            snapshot, binding
                        )
                control_values = dict(current_binding_values)
            provisional_binding_values = dict(current_binding_values)
            for binding in binding_specs:
                if binding.value_source == "setup_output":
                    provisional_binding_values[binding.binding_id] = (
                        f"<setup-output:{binding.binding_id}>"
                    )
            spec, diff = build_replay_spec(
                snapshot,
                mutations,
                volatile_bindings=binding_specs,
                binding_values=provisional_binding_values,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise BrowserServiceError(
                "replay_source_invalid",
                str(exc),
                409,
            ) from exc
        source_url = str(snapshot.get("url", ""))
        source_method = str(snapshot.get("method", "GET")).upper()
        source_resource_type = str(snapshot.get("resourceType", "fetch"))
        source_content_type = response_content_type(snapshot)
        source_is_stream = control_payload.response_mode in {
            "sse",
            "ndjson",
            "raw_stream",
        } or (
            control_payload.response_mode == "auto"
            and source_content_type
            in {"text/event-stream", "application/x-ndjson", "application/ndjson"}
        )
        matcher = RequestMatcher(
            url_contains=source_url.split("?", 1)[0],
            method=source_method,
            resource_types=[source_resource_type] if source_resource_type else [],
        )
        primary = PrimaryRequest(
            url_contains=matcher.url_contains,
            method=matcher.method,
            resource_types=matcher.resource_types,
            mime_types=[source_content_type] if source_content_type else [],
            expected_min_matches=1,
            expected_max_matches=5,
            allow_supporting_failures=True,
            include_in_flight=False,
        )
        selectors: list[Any] = list(control_payload.network_evidence)
        if not selectors:
            selectors = [
                {
                    "selector_id": "replay_request",
                    "matcher": matcher.model_dump(mode="json", exclude_none=True),
                    "max_matches": 20,
                    "export_parts": ["all"],
                    "include_initiator": True,
                }
            ]
        capture = control_payload.capture.model_dump(mode="json")
        requirements = control_payload.requirements.model_dump(mode="json")
        wait_for = control_payload.wait_for
        if source_is_stream:
            capture["stream"] = True
            requirements["require_raw_capture"] = True
            requirements["require_semantic_parse"] = not control_payload.raw_only
            requirements["require_artifacts"] = True
        spec["responseControl"] = {
            "maxResponseBytes": control_payload.max_response_bytes,
            "idleTimeoutMs": control_payload.stream_idle_timeout_ms,
            "responseMode": control_payload.response_mode,
            "terminalConditions": [
                item.model_dump(mode="json", exclude_none=True)
                for item in control_payload.terminal_conditions
            ],
            "doneMarker": (control_payload.default_done_marker if source_is_stream else None),
            "doneEventName": (
                control_payload.default_done_event_name if source_is_stream else None
            ),
        }
        spec["transport"] = control_payload.transport.model_dump(mode="json")
        if isinstance(control_manifest, dict):
            control_series = control_manifest.get("series")
            control_series = control_series if isinstance(control_series, dict) else {}
            series = {
                **control_series,
                "scenario_type": f"treatment_{mutation.type}",
                "predecessor_experiment_id": request_payload.control_experiment_id,
                "sequence_index": int(control_series.get("sequence_index") or 0) + 1,
            }
            objective = f"Treatment for {request_payload.control_experiment_id}: {mutation.type}"
        elif replay_mode == "exploratory":
            series = control_payload.series.model_dump(mode="json", exclude_none=True)
            series["scenario_type"] = series.get("scenario_type") or "exploratory_replay"
            objective = control_payload.objective
        else:
            series = control_payload.series.model_dump(mode="json", exclude_none=True)
            objective = control_payload.objective
        normalized = CaptureFlowPayload.model_validate(
            {
                "session_id": control_payload.session_id,
                "objective": objective,
                "target": control_payload.target.model_dump(mode="json", exclude_none=True),
                "primary_request": primary.model_dump(mode="json"),
                "flow": [
                    item.model_dump(mode="json", exclude_none=True)
                    for item in control_payload.verification_flow
                ],
                "wait_for": (
                    wait_for.model_dump(mode="json", exclude_none=True) if wait_for else None
                ),
                "execution_mode": control_payload.execution_mode,
                "deadline_ms": control_payload.deadline_ms,
                "job_timeout_ms": control_payload.job_timeout_ms,
                "capture": capture,
                "requirements": requirements,
                "network_evidence": [
                    item.model_dump(mode="json", exclude_none=True)
                    if hasattr(item, "model_dump")
                    else item
                    for item in selectors
                ],
                "series": series,
            }
        )
        replay_attempt_id = f"replay_{uuid.uuid4().hex}"
        return normalized, {
            "source_experiment_id": control_payload.source_experiment_id,
            "source_evidence_id": control_payload.source_evidence_id,
            "source_snapshot_path": snapshot_path,
            "source_evidence": source_evidence,
            "source_content_type": source_content_type,
            "source_is_stream": source_is_stream,
            "replay_mode": replay_mode,
            "control_experiment_id": (
                request_payload.control_experiment_id
                if isinstance(request_payload, ReplayTreatmentPayload)
                else None
            ),
            "control_http_status": (
                control_manifest.get("replay_http_status")
                if isinstance(control_manifest, dict)
                else None
            ),
            "volatile_bindings": [
                item.model_dump(mode="json", exclude_none=True) for item in binding_specs
            ],
            "control_volatile_binding_values": control_values,
            "current_volatile_binding_values": current_binding_values,
            "pair_protocol": pair_protocol,
            "pair_protocol_hash": pair_protocol_hash,
            "setup_flow": [
                step.model_dump(mode="json", exclude_none=True)
                for step in control_payload.setup_flow
            ],
            "setup_outputs": [
                item.model_dump(mode="json", exclude_none=True)
                for item in control_payload.setup_outputs
            ],
            "_setup_flow_steps": list(control_payload.setup_flow),
            "_setup_outputs": list(control_payload.setup_outputs),
            "_source_snapshot": snapshot,
            "_binding_specs": binding_specs,
            "ignored_cookie_names": list(control_payload.ignored_cookie_names),
            "ignored_context_headers": list(control_payload.ignored_context_headers),
            "normalize_wire_order": control_payload.normalize_wire_order,
            "environment_comparison": control_payload.environment_comparison.model_dump(
                mode="json"
            ),
            "transport": control_payload.transport.model_dump(mode="json"),
            "mutations": mutations,
            "mutation": mutation,
            "replay_attempt_id": replay_attempt_id,
            "expected_request_body_canonical_sha256": (
                request_body_canonical_sha256_from_spec(spec)
            ),
            "spec": spec,
            "diff": diff,
        }

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
                        "raw_capture_integrity": request.get("rawCaptureIntegrity"),
                        "semantic_parse_integrity": request.get("semanticParseIntegrity"),
                        "request_snapshot_integrity": request.get("requestSnapshotIntegrity"),
                        "artifact_integrity": request.get("artifactIntegrity"),
                    }
                )
        health = manifest.get("capture_health")
        return {
            "experiment_id": manifest.get("experiment_id"),
            "session_id": manifest.get("session_id"),
            "operation": manifest.get("operation"),
            "status": manifest.get("status"),
            "execution_integrity": manifest.get("execution_integrity"),
            "evidence_integrity": manifest.get("evidence_integrity"),
            "causal_comparability": manifest.get("causal_comparability"),
            "inference_eligibility": manifest.get("inference_eligibility"),
            "collector_integrity": manifest.get("collector_integrity"),
            "primary_request_integrity": manifest.get("primary_request_integrity"),
            "primary_integrity_dimensions": manifest.get("primary_integrity_dimensions"),
            "primary_requests": request_summaries,
            "primary_request_count": (
                len(primary_requests) if isinstance(primary_requests, list) else 0
            ),
            "capture_health": dict(health) if isinstance(health, dict) else {},
            "series": (
                dict(manifest["series"]) if isinstance(manifest.get("series"), dict) else {}
            ),
            "evidence_count": len(manifest.get("evidence", []))
            if isinstance(manifest.get("evidence"), list)
            else 0,
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
        if isinstance(request, CancelExperimentRequest):
            return await self._cancel_experiment(request)
        if isinstance(request, SaveScriptSourceRequest):
            return await self._save_script_source(request)
        if isinstance(request, OpenSessionRequest):
            session_id = request.payload.session_id or f"sess_{uuid.uuid4().hex[:12]}"
            owner_id = f"open_{uuid.uuid4().hex}"
            await self._reserve_browser_operation(
                session_id=session_id,
                owner_id=owner_id,
                operation="open_session",
            )
            try:
                return await self._open_session(request, session_id=session_id)
            finally:
                await self._release_browser_operation(owner_id)
        if isinstance(request, CloseSessionRequest):
            owner_id = f"close_{uuid.uuid4().hex}"
            await self._reserve_browser_operation(
                session_id=request.payload.session_id,
                owner_id=owner_id,
                operation="close_session",
            )
            try:
                return await self._close_session(request)
            finally:
                await self._release_browser_operation(owner_id)
        if isinstance(
            request,
            (CaptureFlowRequest, CaptureBaselineRequest, ReplayRequestRequest),
        ):
            replay_plan: dict[str, Any] | None = None
            if isinstance(request, ReplayRequestRequest):
                payload, replay_plan = self._prepare_replay_execution(request)
            else:
                payload = request.payload
            experiment_id = self.experiments.new_experiment_id()
            await self._reserve_browser_operation(
                session_id=payload.session_id,
                owner_id=experiment_id,
                operation=request.operation,
                experiment_id=experiment_id,
            )
            if payload.execution_mode == "job":
                try:
                    return self._start_capture_job(
                        request,
                        experiment_id=experiment_id,
                        payload=payload,
                        replay_plan=replay_plan,
                    )
                except Exception:
                    await self._release_browser_operation(experiment_id)
                    raise
            deadline = Deadline(payload.deadline_ms)
            try:
                experiment_id, experiment_dir, manifest = self.experiments.create_experiment(
                    session_id=payload.session_id,
                    operation=request.operation,
                    objective=payload.objective,
                    deadline=deadline,
                    experiment_id=experiment_id,
                )
                manifest["execution_mode"] = "sync"
                manifest["primary_request_matcher"] = payload.primary_request.model_dump(
                    mode="json", exclude_none=True
                )
                self._validate_and_store_series(
                    session_id=payload.session_id,
                    manifest=manifest,
                    payload=payload,
                )
                if replay_plan:
                    manifest["replay_source"] = {
                        "source_experiment_id": replay_plan["source_experiment_id"],
                        "source_evidence_id": replay_plan["source_evidence_id"],
                    }
                    manifest["replay"] = {
                        "replay_mode": replay_plan["replay_mode"],
                        "source_experiment_id": replay_plan["source_experiment_id"],
                        "source_evidence_id": replay_plan["source_evidence_id"],
                        "control_experiment_id": replay_plan["control_experiment_id"],
                        "source_content_type": replay_plan["source_content_type"],
                        "source_is_stream": replay_plan["source_is_stream"],
                        "volatile_bindings": replay_plan["volatile_bindings"],
                        "control_volatile_binding_values": replay_plan[
                            "control_volatile_binding_values"
                        ],
                        "current_volatile_binding_values": replay_plan[
                            "current_volatile_binding_values"
                        ],
                        "pair_protocol": replay_plan["pair_protocol"],
                        "pair_protocol_hash": replay_plan["pair_protocol_hash"],
                        "replay_attempt_id": replay_plan["replay_attempt_id"],
                        "expected_request_body_canonical_sha256": replay_plan[
                            "expected_request_body_canonical_sha256"
                        ],
                    }
                self.experiments.write_manifest(experiment_id, manifest)
                return await self._capture_flow(
                    request,
                    deadline=deadline,
                    prepared=(experiment_id, experiment_dir, manifest),
                    payload=payload,
                    replay_plan=replay_plan,
                )
            finally:
                await self._release_browser_operation(experiment_id)
        raise BrowserServiceError("unsupported_operation", "Unsupported browser operation", 400)

    async def _cancel_experiment(
        self,
        request: CancelExperimentRequest,
    ) -> BrowserActionResponse:
        experiment_id = request.payload.experiment_id
        manifest = self.experiments.load_manifest(experiment_id)
        if manifest.get("session_id") != request.payload.session_id:
            raise BrowserServiceError(
                "experiment_session_mismatch",
                "Experiment does not belong to the supplied session.",
                409,
            )
        task = self._jobs.get(experiment_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            manifest = self.experiments.load_manifest(experiment_id)
        return BrowserActionResponse(
            operation=request.operation,
            status=(
                str(manifest.get("status"))
                if manifest.get("status")
                in {"running", "completed", "failed", "partial", "interrupted"}
                else "partial"
            ),
            session_id=request.payload.session_id,
            experiment_id=experiment_id,
            result={
                "experiment": self._experiment_summary(manifest),
                "manifest_relative_path": self._manifest_relative_path(experiment_id),
                "collector_cleanup": (
                    (manifest.get("capture_health") or {}).get("collector_cleanup")
                    if isinstance(manifest.get("capture_health"), dict)
                    else None
                ),
            },
        )

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
                if manifest_status in {"running", "completed", "failed", "partial", "interrupted"}
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
        if isinstance(request, ListEvidenceRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            evidence = self._evidence_index(manifest)
            if request.payload.kind:
                evidence = [item for item in evidence if item.get("kind") == request.payload.kind]
            evidence = evidence[: request.payload.limit]
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"evidence": evidence, "count": len(evidence)},
            )
        if isinstance(request, GetNetworkEvidenceRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            item = self._find_evidence(manifest, request.payload.evidence_id)
            if item.get("kind") != "network_request":
                raise BrowserServiceError(
                    "evidence_kind_mismatch",
                    "The requested evidence is not network_request evidence.",
                    409,
                )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"evidence": item},
            )
        if isinstance(request, GetRequestShapeRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            item = self._find_evidence(manifest, request.payload.evidence_id)
            if item.get("kind") != "network_request":
                raise BrowserServiceError(
                    "evidence_kind_mismatch",
                    "The requested evidence is not network_request evidence.",
                    409,
                )
            paths = item.get("artifact_paths")
            paths = paths if isinstance(paths, dict) else {}
            shape_relative = paths.get("request_shape")
            redacted_relative = paths.get("request_body_redacted")
            if not isinstance(shape_relative, str):
                raise BrowserServiceError(
                    "request_shape_missing",
                    "The network evidence has no JSON request shape artifact.",
                    404,
                )
            try:
                shape_path = (self.experiments.root / shape_relative).resolve()
                shape_path.relative_to(self.experiments.root)
                shape = json.loads(shape_path.read_text(encoding="utf-8"))
                redacted = None
                if request.payload.include_redacted_body and isinstance(redacted_relative, str):
                    redacted_path = (self.experiments.root / redacted_relative).resolve()
                    redacted_path.relative_to(self.experiments.root)
                    redacted = self._bounded_redacted_subtree(
                        json.loads(redacted_path.read_text(encoding="utf-8")),
                        path_prefix=request.payload.path_prefix,
                        max_depth=request.payload.max_depth,
                        max_array_items=request.payload.max_array_items,
                    )
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise BrowserServiceError(
                    "request_shape_invalid",
                    "The saved request shape artifact is unavailable or invalid.",
                    409,
                ) from exc
            all_paths = shape.get("paths")
            all_paths = all_paths if isinstance(all_paths, dict) else {}
            filtered = self._filter_shape_paths(
                all_paths,
                path_prefix=request.payload.path_prefix,
                max_depth=request.payload.max_depth,
                max_array_items=request.payload.max_array_items,
            )
            start = request.payload.page_idx * request.payload.page_size
            page = filtered[start : start + request.payload.page_size]
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={
                    "evidence_id": request.payload.evidence_id,
                    "request_shape": {
                        "format": shape.get("format", "json-pointer-v1"),
                        "path_prefix": request.payload.path_prefix,
                        "paths": dict(page),
                    },
                    "request_body_redacted": redacted,
                    "pagination": {
                        "page_idx": request.payload.page_idx,
                        "page_size": request.payload.page_size,
                        "total_paths": len(filtered),
                        "has_next_page": start + len(page) < len(filtered),
                    },
                    "limits": {
                        "max_depth": request.payload.max_depth,
                        "max_array_items": request.payload.max_array_items,
                        "include_redacted_body": (request.payload.include_redacted_body),
                    },
                },
            )
        if isinstance(request, GetRequestInitiatorRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            item = self._find_evidence(manifest, request.payload.evidence_id)
            paths = item.get("artifact_paths")
            paths = paths if isinstance(paths, dict) else {}
            relative = paths.get("initiator")
            if not isinstance(relative, str):
                raise BrowserServiceError(
                    "initiator_evidence_missing",
                    "The network evidence has no saved initiator artifact.",
                    404,
                )
            path = (self.experiments.root / relative).resolve()
            try:
                path.relative_to(self.experiments.root)
                initiator = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise BrowserServiceError(
                    "initiator_evidence_invalid",
                    "The saved initiator artifact is unavailable or invalid.",
                    409,
                ) from exc
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={
                    "evidence_id": request.payload.evidence_id,
                    "initiator": initiator,
                },
            )
        if isinstance(request, ListConsoleErrorsRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            evidence = [
                item
                for item in self._evidence_index(manifest)
                if item.get("kind") == "console_message"
            ][: request.payload.limit]
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"console_errors": evidence, "count": len(evidence)},
            )
        if isinstance(request, SearchScriptsRequest):

            async def search(deadline: Deadline) -> dict[str, Any]:
                return await self.js_reverse.search_scripts(
                    request.payload.query,
                    deadline,
                    url_filter=request.payload.url_filter,
                    max_results=request.payload.max_results,
                    exclude_minified=request.payload.exclude_minified,
                )

            result = await self._run_aligned_inspection(
                session_id=request.payload.session_id,
                operation=request.operation,
                callback=search,
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=request.payload.session_id,
                result={"search": result},
            )
        if isinstance(request, GetScriptSourceRequest):

            async def source(deadline: Deadline) -> dict[str, Any]:
                return await self.js_reverse.get_script_source(
                    deadline,
                    url=request.payload.url,
                    script_id=request.payload.script_id,
                    start_line=request.payload.start_line,
                    end_line=request.payload.end_line,
                    offset=request.payload.offset,
                    length=request.payload.length,
                )

            result = await self._run_aligned_inspection(
                session_id=request.payload.session_id,
                operation=request.operation,
                callback=source,
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=request.payload.session_id,
                result={"source": result},
            )
        if isinstance(request, GetStreamStatusRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            runtime = self._manifest_stream_runtime(manifest)
            expected_uuid = runtime.get("capture_uuid")
            if (
                request.payload.capture_uuid is not None
                and expected_uuid is not None
                and request.payload.capture_uuid != expected_uuid
            ):
                raise BrowserServiceError(
                    "capture_uuid_mismatch",
                    "The supplied capture UUID does not match the experiment manifest.",
                    409,
                )
            owner = self.coordinator.browser_owner
            capture_id = runtime.get("capture_id")
            recorded_generation = runtime.get("transport_generation")
            live = (
                manifest.get("status") == "running"
                and owner is not None
                and owner.experiment_id == request.payload.experiment_id
                and isinstance(capture_id, int)
                and recorded_generation == self._transport_generation()
            )
            if live:
                status = await self.js_reverse.get_stream_status(int(capture_id), Deadline(10_000))
                returned_capture = status.get("capture")
                returned_uuid = (
                    returned_capture.get("captureUuid")
                    if isinstance(returned_capture, dict)
                    else None
                )
                if expected_uuid and returned_uuid != expected_uuid:
                    raise BrowserServiceError(
                        "capture_identity_mismatch",
                        "Live MCP capture identity does not match the experiment manifest.",
                        409,
                    )
                source = "live-mcp"
            else:
                persisted = manifest.get("stream_status")
                status = (
                    dict(persisted)
                    if isinstance(persisted, dict)
                    else {
                        "capture": {
                            "captureUuid": expected_uuid,
                            "relativeDir": runtime.get("capture_relative_dir"),
                            "status": manifest.get("status"),
                        },
                        "requests": manifest.get("primary_requests", []),
                    }
                )
                source = "manifest"
            return BrowserActionResponse(
                operation=request.operation,
                status=("running" if manifest.get("status") == "running" else "completed"),
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"stream": status, "source": source},
            )
        raise BrowserServiceError("unsupported_operation", "Unsupported inspect operation", 400)

    async def _open_session(
        self,
        request: OpenSessionRequest,
        *,
        session_id: str,
    ) -> BrowserActionResponse:
        payload = request.payload
        deadline = Deadline(payload.deadline_ms)
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
        if self.private_mcp_browser_endpoint and endpoint != self.private_mcp_browser_endpoint:
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
        page = await self.playwright.current_page(str(session["playwright_session_ref"]), deadline)
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
                str(session["js_reverse_page_id"]) if session.get("js_reverse_page_id") else None
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
                    response_observed=bool(request_value.get("response_observed", False)),
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
                    primary_event_source=str(request_value.get("primary_event_source") or "none"),
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
        return {
            "complete": 0,
            "not_required": 0,
            "not_applicable_protocol_rejection": 0,
            "not_applicable_non_stream_response": 0,
            "semantic-only": 1,
            "partial": 2,
            "failed": 3,
        }.get(value, 2)

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
            if isinstance(item, dict) and JsReverseMcpAdapter._request_matches(item, matcher)
        ]
        if not requests and not payload.capture.stream:
            requests = [
                {
                    **item,
                    "integrityStatus": item.get("integrityStatus", "partial"),
                    "evidenceSource": "network-summary",
                }
                for item in network_payload.get("requests", [])
                if isinstance(item, dict) and JsReverseMcpAdapter._request_matches(item, matcher)
            ]
        count_ok = (
            payload.primary_request.expected_min_matches
            <= len(requests)
            <= payload.primary_request.expected_max_matches
        )
        if not requests:
            if payload.primary_request.expected_min_matches == 0:
                return (
                    requests,
                    "complete",
                    count_ok,
                    {
                        "raw_capture": "complete",
                        "semantic_parse": "complete",
                        "request_snapshot": "complete",
                        "artifacts": "complete",
                    },
                )
            integrity = "failed"
            return (
                requests,
                integrity,
                count_ok,
                {
                    "raw_capture": "failed" if payload.capture.stream else "partial",
                    "semantic_parse": "failed" if payload.capture.stream else "partial",
                    "request_snapshot": "failed" if payload.capture.stream else "partial",
                    "artifacts": "failed" if payload.capture.stream else "partial",
                },
            )
        dimensions = {
            "raw_capture": (
                max(
                    (str(item.get("rawCaptureIntegrity", "partial")) for item in requests),
                    key=self._integrity_severity,
                )
                if payload.capture.stream
                else "not_required"
            ),
            "semantic_parse": (
                max(
                    (str(item.get("semanticParseIntegrity", "partial")) for item in requests),
                    key=self._integrity_severity,
                )
                if payload.capture.stream
                else "not_required"
            ),
            "request_snapshot": max(
                (
                    str(
                        item.get("networkSnapshotIntegrity")
                        or item.get("requestSnapshotIntegrity", "partial")
                    )
                    for item in requests
                ),
                key=self._integrity_severity,
            ),
            "artifacts": max(
                (str(item.get("artifactIntegrity", "partial")) for item in requests),
                key=self._integrity_severity,
            ),
        }
        applicable_values = [
            value
            for value in dimensions.values()
            if value
            not in {
                "not_required",
                "not_applicable_protocol_rejection",
                "not_applicable_non_stream_response",
            }
        ]
        integrity = (
            max(applicable_values, key=self._integrity_severity)
            if applicable_values
            else "complete"
        )
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
            result.step_id: result for result in step_results if result.status == "completed"
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
                    and item.get("condition_type") in {"first_event", "event_predicate"}
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
                stop_wall_ms = int(datetime.fromisoformat(result.ended_at).timestamp() * 1000)
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
            and initial_alignment.js_reverse_page_id == post_alignment.js_reverse_page_id
            and initial_alignment.playwright_page.url == post_alignment.playwright_page.url
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
            same_request_observed = bool(request_ids & before_ids) and bool(request_ids & after_ids)
            expected = within_window and page_remained_aligned and same_request_observed
            classification = {
                "request_id": request.get("cdpRequestId"),
                "persistent_request_id": request.get("persistentRequestId"),
                "source_terminal_reason": "network_canceled",
                "classification": (
                    "expected_user_cancel" if expected else "unclassified_network_cancel"
                ),
                "stop_step_id": nearest["step_id"],
                "stop_delta_ms": delta_ms,
                "within_stop_window": within_window,
                "page_remained_aligned": page_remained_aligned,
                "same_request_observed": same_request_observed,
                "stream_before_stop": ((nearest.get("before") or {}).get("matched_event")),
                "stream_after_stop": ((nearest.get("after") or {}).get("matched_event")),
            }
            request["experimentCancellationClassification"] = classification["classification"]
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
        self,
        request: CaptureFlowRequest | CaptureBaselineRequest | ReplayRequestRequest,
        *,
        experiment_id: str,
        payload: CaptureFlowPayload,
        replay_plan: dict[str, Any] | None,
    ) -> BrowserActionResponse:
        session = self._get_session(payload.session_id)
        if session.get("status") != "open":
            raise BrowserServiceError("session_closed", "Browser session is not open", 409)
        deadline = Deadline(payload.job_timeout_ms)
        experiment_id, experiment_dir, manifest = self.experiments.create_experiment(
            session_id=payload.session_id,
            operation=request.operation,
            objective=payload.objective,
            deadline=deadline,
            experiment_id=experiment_id,
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
        self._validate_and_store_series(
            session_id=payload.session_id,
            manifest=manifest,
            payload=payload,
        )
        if replay_plan:
            manifest["replay_source"] = {
                "source_experiment_id": replay_plan["source_experiment_id"],
                "source_evidence_id": replay_plan["source_evidence_id"],
            }
            manifest["replay"] = {
                "replay_mode": replay_plan["replay_mode"],
                "source_experiment_id": replay_plan["source_experiment_id"],
                "source_evidence_id": replay_plan["source_evidence_id"],
                "control_experiment_id": replay_plan["control_experiment_id"],
                "source_content_type": replay_plan["source_content_type"],
                "source_is_stream": replay_plan["source_is_stream"],
                "volatile_bindings": replay_plan["volatile_bindings"],
                "control_volatile_binding_values": replay_plan["control_volatile_binding_values"],
                "current_volatile_binding_values": replay_plan["current_volatile_binding_values"],
                "pair_protocol": replay_plan["pair_protocol"],
                "pair_protocol_hash": replay_plan["pair_protocol_hash"],
                "replay_attempt_id": replay_plan["replay_attempt_id"],
                "expected_request_body_canonical_sha256": replay_plan[
                    "expected_request_body_canonical_sha256"
                ],
            }
        self.experiments.write_manifest(experiment_id, manifest)
        task = asyncio.create_task(
            self._run_capture_job(
                request,
                deadline=deadline,
                prepared=(experiment_id, experiment_dir, manifest),
                payload=payload,
                replay_plan=replay_plan,
            ),
            name=f"browser-experiment-{experiment_id}",
        )
        self._jobs[experiment_id] = task
        self._active_session_jobs[payload.session_id] = experiment_id

        def clear_job(_task: asyncio.Task[None]) -> None:
            self._jobs.pop(experiment_id, None)
            if self._active_session_jobs.get(payload.session_id) == experiment_id:
                self._active_session_jobs.pop(payload.session_id, None)

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
        request: CaptureFlowRequest | CaptureBaselineRequest | ReplayRequestRequest,
        *,
        deadline: Deadline,
        prepared: tuple[str, Path, dict[str, Any]],
        payload: CaptureFlowPayload,
        replay_plan: dict[str, Any] | None,
    ) -> None:
        experiment_id = prepared[0]
        try:
            try:
                await self._capture_flow(
                    request,
                    deadline=deadline,
                    prepared=prepared,
                    payload=payload,
                    replay_plan=replay_plan,
                )
            except asyncio.CancelledError:
                manifest = self.experiments.load_manifest(experiment_id)
                manifest["status"] = "interrupted"
                manifest["errors"] = [
                    *(manifest.get("errors") if isinstance(manifest.get("errors"), list) else []),
                    "Background experiment task was canceled.",
                ]
                self.experiments.write_manifest(experiment_id, manifest)
                raise
            except Exception as exc:
                manifest = self.experiments.load_manifest(experiment_id)
                manifest["status"] = "failed"
                manifest["errors"] = [
                    *(manifest.get("errors") if isinstance(manifest.get("errors"), list) else []),
                    str(exc)[:4000],
                ]
                self.experiments.write_manifest(experiment_id, manifest)
        finally:
            await self._release_browser_operation(experiment_id)

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
        stream_start_status: str,
        capture_transport_generation: int | None,
        trace_started: bool,
        execution_deadline: Deadline,
        canceled: bool,
    ) -> dict[str, Any]:
        cleanup_deadline = Deadline(self.FINALIZE_GRACE_MS)
        entered_reserve = execution_deadline.remaining_ms() <= self.FINALIZE_RESERVE_MS
        result: dict[str, Any] = {
            "stop_payload": {},
            "final_status_payload": {},
            "trace_paths": [],
            "screenshot_paths": [],
            "snapshot_paths": [],
            "network_payload": {},
            "collector_stopped": (
                not payload.capture.stream
                or stream_start_status in {"not_attempted", "failed_before_send"}
            ),
            "collector_cleanup": (
                "not_required"
                if not payload.capture.stream
                or stream_start_status in {"not_attempted", "failed_before_send"}
                else "unknown"
            ),
            "orphan_capture_id": None,
            "warnings": [],
            "errors": [],
            "entered_finalize_reserve": entered_reserve,
        }
        can_stop_live_capture = (
            capture_id is not None
            and stream_start_status == "confirmed"
            and capture_transport_generation == self._transport_generation()
        )
        if can_stop_live_capture:
            try:
                result["stop_payload"] = await self.js_reverse.stop_stream_capture(
                    capture_id,
                    cleanup_deadline.child(6_000),
                )
                result["collector_stopped"] = True
                result["collector_cleanup"] = "completed"
            except Exception as exc:
                result["errors"].append(f"stream stop: {str(exc)[:3500]}")
                result["orphan_capture_id"] = capture_id
                message = str(exc).lower()
                result["collector_cleanup"] = (
                    "timed_out" if "timed out" in message or "deadline" in message else "unknown"
                )
            else:
                if not canceled and cleanup_deadline.remaining_ms() > 500:
                    try:
                        result["final_status_payload"] = await self.js_reverse.get_stream_status(
                            capture_id,
                            cleanup_deadline.child(1_500),
                        )
                    except Exception as exc:
                        result["warnings"].append(f"post-stop status: {str(exc)[:3500]}")
                if not result["final_status_payload"] and result["stop_payload"]:
                    result["final_status_payload"] = dict(result["stop_payload"])
        elif payload.capture.stream and stream_start_status in {
            "confirmed",
            "outcome_unknown",
        }:
            result["collector_stopped"] = False
            result["collector_cleanup"] = "unknown"
            if capture_id is not None:
                result["orphan_capture_id"] = capture_id
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
        if not canceled and not entered_reserve and execution_deadline.remaining_ms() > 1_000:
            if payload.capture.network or payload.network_evidence:
                try:
                    result["network_payload"] = await self.js_reverse.list_network_requests(
                        RequestMatcher(),
                        execution_deadline.child(min(2_000, execution_deadline.remaining_ms())),
                    )
                except Exception as exc:
                    result["warnings"].append(f"network summary: {str(exc)[:3500]}")
            if payload.capture.screenshots and execution_deadline.remaining_ms() > 500:
                try:
                    result["screenshot_paths"].append(
                        await self.playwright.capture_screenshot(
                            session_id,
                            experiment_dir,
                            "after-flow",
                            execution_deadline.child(min(2_000, execution_deadline.remaining_ms())),
                        )
                    )
                except Exception as exc:
                    result["warnings"].append(f"final screenshot: {str(exc)[:3500]}")
            if payload.capture.page_snapshots and execution_deadline.remaining_ms() > 500:
                try:
                    result["snapshot_paths"].append(
                        await self.playwright.capture_snapshot(
                            session_id,
                            experiment_dir,
                            "after-flow",
                            execution_deadline.child(min(2_000, execution_deadline.remaining_ms())),
                        )
                    )
                except Exception as exc:
                    result["warnings"].append(f"final page snapshot: {str(exc)[:3500]}")
        return result

    async def _capture_flow(
        self,
        request: CaptureFlowRequest | CaptureBaselineRequest | ReplayRequestRequest,
        *,
        deadline: Deadline | None = None,
        prepared: tuple[str, Path, dict[str, Any]] | None = None,
        payload: CaptureFlowPayload | None = None,
        replay_plan: dict[str, Any] | None = None,
    ) -> BrowserActionResponse:
        if payload is None:
            if isinstance(request, ReplayRequestRequest):
                payload, replay_plan = self._prepare_replay_execution(request)
            else:
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
                        "manifest_relative_path": self._manifest_relative_path(experiment_id),
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
                        "manifest_relative_path": self._manifest_relative_path(experiment_id),
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
            capture_transport_generation: int | None = None
            stream_start_status = "not_attempted"
            start_payload: dict[str, Any] = {}
            final_status_payload: dict[str, Any] = {}
            stop_payload: dict[str, Any] = {}
            wait_result: dict[str, Any] | None = None
            trace_paths: list[str] = []
            screenshot_paths: list[str] = []
            snapshot_paths: list[str] = []
            network_payload: dict[str, Any] = {}
            network_checkpoint_value: dict[str, Any] = {}
            console_checkpoint_value: dict[str, Any] = {}
            replay_result: dict[str, Any] = {}
            replay_response: Any = None
            replay_http_status: int | None = None
            replay_response_content_type: str | None = None
            post_response_alignment: AlignmentResult | None = None
            pre_dispatch_alignment: AlignmentResult = alignment
            replay_artifacts: list[dict[str, Any]] = []
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
                if payload.capture.network or payload.network_evidence:
                    try:
                        checkpoint_requests = await self._all_network_requests(
                            self._operation_deadline(
                                deadline,
                                2_000,
                                "network checkpoint",
                            )
                        )
                        network_checkpoint_value = network_checkpoint(
                            checkpoint_requests,
                            generation=self._transport_generation(),
                        )
                    except Exception as exc:
                        warnings.append(f"network checkpoint: {str(exc)[:2000]}")
                if payload.capture.console_errors:
                    try:
                        console_checkpoint_value = await self._console_checkpoint(
                            self._operation_deadline(
                                deadline,
                                2_000,
                                "console checkpoint",
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"console checkpoint: {str(exc)[:2000]}")
                if payload.capture.trace:
                    await self.playwright.start_trace(
                        session_id,
                        self._operation_deadline(deadline, 3_000, "trace start"),
                    )
                    trace_started = True
                if payload.capture.stream:
                    stream_start_status = "failed_before_send"
                    try:
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
                    except asyncio.CancelledError as exc:
                        capture_transport_generation = int(
                            getattr(
                                exc,
                                "mcp_transport_generation",
                                self._transport_generation(),
                            )
                        )
                        stream_start_status = (
                            "outcome_unknown"
                            if bool(getattr(exc, "mcp_outcome_unknown", False))
                            else "failed_before_send"
                        )
                        discovered = (
                            self._discover_capture_metadata(experiment_id)
                            if stream_start_status == "outcome_unknown"
                            else None
                        )
                        if discovered:
                            capture_id = (
                                int(discovered["capture_id"])
                                if isinstance(discovered.get("capture_id"), int)
                                else None
                            )
                            capture_uuid = discovered.get("capture_uuid")
                            capture_relative_dir = discovered.get("capture_relative_dir")
                            capture_metadata_artifact_id = discovered.get(
                                "capture_metadata_artifact_id"
                            )
                        self._write_stream_runtime(
                            experiment_id=experiment_id,
                            manifest=manifest,
                            start_status=stream_start_status,
                            capture_id=capture_id,
                            capture_uuid=(str(capture_uuid) if capture_uuid is not None else None),
                            capture_relative_dir=(
                                str(capture_relative_dir)
                                if capture_relative_dir is not None
                                else None
                            ),
                            capture_metadata_artifact_id=(
                                str(capture_metadata_artifact_id)
                                if capture_metadata_artifact_id is not None
                                else None
                            ),
                            transport_generation=capture_transport_generation,
                        )
                        raise
                    except McpToolCallError as exc:
                        capture_transport_generation = exc.transport_generation
                        stream_start_status = (
                            "outcome_unknown" if exc.outcome_unknown else "failed_before_send"
                        )
                        discovered = (
                            self._discover_capture_metadata(experiment_id)
                            if stream_start_status == "outcome_unknown"
                            else None
                        )
                        if discovered:
                            capture_id = (
                                int(discovered["capture_id"])
                                if isinstance(discovered.get("capture_id"), int)
                                else None
                            )
                            capture_uuid = discovered.get("capture_uuid")
                            capture_relative_dir = discovered.get("capture_relative_dir")
                            capture_metadata_artifact_id = discovered.get(
                                "capture_metadata_artifact_id"
                            )
                        self._write_stream_runtime(
                            experiment_id=experiment_id,
                            manifest=manifest,
                            start_status=stream_start_status,
                            capture_id=capture_id,
                            capture_uuid=(str(capture_uuid) if capture_uuid is not None else None),
                            capture_relative_dir=(
                                str(capture_relative_dir)
                                if capture_relative_dir is not None
                                else None
                            ),
                            capture_metadata_artifact_id=(
                                str(capture_metadata_artifact_id)
                                if capture_metadata_artifact_id is not None
                                else None
                            ),
                            transport_generation=capture_transport_generation,
                        )
                        raise
                    capture = start_payload.get("capture")
                    if not isinstance(capture, dict) or not capture.get("captureId"):
                        raise BrowserServiceError(
                            "stream_start_invalid", "Stream collector returned no capture ID", 502
                        )
                    capture_id = int(capture["captureId"])
                    capture_transport_generation = self._transport_generation()
                    stream_start_status = "confirmed"
                    capture_uuid = (
                        str(capture["captureUuid"]) if capture.get("captureUuid") else None
                    )
                    capture_relative_dir = (
                        str(capture["relativeDir"]) if capture.get("relativeDir") else None
                    )
                    metadata_artifact = capture.get("metadataArtifact")
                    if isinstance(metadata_artifact, dict) and metadata_artifact.get("artifactId"):
                        capture_metadata_artifact_id = str(metadata_artifact["artifactId"])
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
                    self._write_stream_runtime(
                        experiment_id=experiment_id,
                        manifest=manifest,
                        start_status=stream_start_status,
                        capture_id=capture_id,
                        capture_uuid=capture_uuid,
                        capture_relative_dir=capture_relative_dir,
                        capture_metadata_artifact_id=capture_metadata_artifact_id,
                        transport_generation=capture_transport_generation,
                    )
                if replay_plan is not None:
                    setup_steps = replay_plan.get("_setup_flow_steps")
                    setup_outputs = replay_plan.get("_setup_outputs")
                    setup_output_checkpoint = (
                        network_checkpoint(
                            await self._all_network_requests(
                                self._operation_deadline(
                                    deadline,
                                    2_500,
                                    "setup output network checkpoint",
                                )
                            ),
                            generation=self._transport_generation(),
                        )
                        if isinstance(setup_outputs, list) and setup_outputs
                        else None
                    )
                    if isinstance(setup_steps, list) and setup_steps:
                        for setup_index, step in enumerate(setup_steps):
                            self._ensure_finalize_reserve(
                                deadline,
                                f"setup step {step.step_id}",
                            )
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
                                                f"checkpoint before setup {step.step_id}",
                                            ),
                                        )
                                    if first_mutation_wall_time_ms is None:
                                        first_mutation_wall_time_ms = int(time.time() * 1000)
                                step_deadline = self._operation_deadline(
                                    deadline,
                                    step.timeout_ms,
                                    f"setup step {step.step_id}",
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
                                            "phase": "setup",
                                            "step_id": step.step_id,
                                            "step_index": setup_index,
                                            "condition_type": step.condition.type,
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
                                            f"Setup condition failed: {step.step_id}",
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
                                canceled_status = (
                                    "canceled"
                                    if step.action in {"wait", "assert", "snapshot"}
                                    else "canceled_outcome_unknown"
                                )
                                step_results.append(
                                    FlowStepResult(
                                        step_id=step.step_id,
                                        status=canceled_status,
                                        started_at=started,
                                        ended_at=utc_now(),
                                        error="The replay setup step was canceled.",
                                    )
                                )
                                raise
                            except Exception as exc:
                                step_results.append(
                                    FlowStepResult(
                                        step_id=step.step_id,
                                        status="failed",
                                        started_at=started,
                                        ended_at=utc_now(),
                                        error=str(exc)[:4000],
                                    )
                                )
                                raise
                        if isinstance(setup_output_checkpoint, dict):
                            (
                                setup_values,
                                setup_output_records,
                            ) = await self._resolve_setup_output_bindings(
                                setup_outputs=setup_outputs,
                                checkpoint=setup_output_checkpoint,
                                experiment_dir=experiment_dir,
                                deadline=self._operation_deadline(
                                    deadline,
                                    8_000,
                                    "resolve setup outputs",
                                ),
                            )
                            replay_plan["current_volatile_binding_values"].update(setup_values)
                            if replay_plan.get("replay_mode") == "control":
                                replay_plan["control_volatile_binding_values"].update(setup_values)
                            rebuilt_spec, rebuilt_diff = build_replay_spec(
                                replay_plan["_source_snapshot"],
                                replay_plan["mutations"],
                                volatile_bindings=replay_plan["_binding_specs"],
                                binding_values=replay_plan["current_volatile_binding_values"],
                            )
                            rebuilt_spec["responseControl"] = replay_plan["spec"]["responseControl"]
                            rebuilt_spec["transport"] = replay_plan["spec"]["transport"]
                            replay_plan["spec"] = rebuilt_spec
                            replay_plan["diff"] = rebuilt_diff
                            replay_plan["expected_request_body_canonical_sha256"] = (
                                request_body_canonical_sha256_from_spec(rebuilt_spec)
                            )
                            replay_plan["setup_output_bindings"] = setup_output_records
                            replay_manifest = manifest.get("replay")
                            if isinstance(replay_manifest, dict):
                                replay_manifest.update(
                                    {
                                        "current_volatile_binding_values": replay_plan[
                                            "current_volatile_binding_values"
                                        ],
                                        "control_volatile_binding_values": replay_plan[
                                            "control_volatile_binding_values"
                                        ],
                                        "expected_request_body_canonical_sha256": replay_plan[
                                            "expected_request_body_canonical_sha256"
                                        ],
                                        "setup_output_bindings": setup_output_records,
                                    }
                                )
                                self.experiments.write_manifest(
                                    experiment_id,
                                    manifest,
                                )
                        try:
                            setup_page = await self.playwright.current_page(
                                session_id,
                                self._operation_deadline(
                                    deadline,
                                    2_500,
                                    "pre-dispatch current page",
                                ),
                            )
                            pre_dispatch_alignment = await self.js_reverse.align_page(
                                setup_page,
                                self._operation_deadline(
                                    deadline,
                                    2_500,
                                    "pre-dispatch page alignment",
                                ),
                                page_id=(
                                    str(session["js_reverse_page_id"])
                                    if session.get("js_reverse_page_id")
                                    else None
                                ),
                            )
                        except Exception as exc:
                            warnings.append(f"pre-dispatch alignment: {str(exc)[:1000]}")
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
                if payload.capture.page_snapshots:
                    try:
                        snapshot_paths.append(
                            await self.playwright.capture_snapshot(
                                session_id,
                                experiment_dir,
                                "before-flow",
                                self._operation_deadline(
                                    deadline,
                                    3_000,
                                    "initial page snapshot",
                                ),
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"initial page snapshot: {str(exc)[:3500]}")
                if replay_plan is not None:
                    if capture_id is not None:
                        stream_checkpoint = await self._stream_checkpoint(
                            capture_id,
                            request_matcher,
                            self._operation_deadline(
                                deadline,
                                1_500,
                                "checkpoint before replay",
                            ),
                        )
                    first_mutation_wall_time_ms = int(time.time() * 1000)
                    replay_dir = experiment_dir / "replay"
                    replay_dir.mkdir(parents=True, exist_ok=True)
                    spec_file = replay_dir / "request-spec.json"
                    diff_file = replay_dir / "request-diff.json"
                    result_file = replay_dir / "response.json"
                    spec_file.write_text(
                        json.dumps(replay_plan["spec"], ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    diff_file.write_text(
                        json.dumps(replay_plan["diff"], ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    started = utc_now()
                    try:
                        replay_plan["dispatch_wall_time_ms"] = int(time.time() * 1000)
                        replay_plan["correlation_window_end_wall_time_ms"] = replay_plan[
                            "dispatch_wall_time_ms"
                        ] + max(1_000, deadline.remaining_ms())
                        replay_manifest = manifest.get("replay")
                        if isinstance(replay_manifest, dict):
                            replay_manifest["dispatch_wall_time_ms"] = replay_plan[
                                "dispatch_wall_time_ms"
                            ]
                            replay_manifest["correlation_window_end_wall_time_ms"] = replay_plan[
                                "correlation_window_end_wall_time_ms"
                            ]
                            self.experiments.write_manifest(experiment_id, manifest)
                        replay_result = await self.js_reverse.evaluate_browser_replay(
                            spec_file,
                            result_file,
                            self._operation_deadline(
                                deadline,
                                20_000,
                                "browser-context replay",
                            ),
                        )
                        if result_file.is_file():
                            try:
                                replay_response = json.loads(
                                    result_file.read_text(encoding="utf-8")
                                )
                                replay_http_status = self._extract_http_status(replay_response)
                                replay_response_content_type = self._extract_response_content_type(
                                    replay_response
                                )
                            except (OSError, json.JSONDecodeError) as exc:
                                warnings.append(f"replay response status: {str(exc)[:1000]}")
                        try:
                            response_page = await self.playwright.current_page(
                                session_id,
                                Deadline(1_500),
                            )
                            post_response_alignment = await self.js_reverse.align_page(
                                response_page,
                                Deadline(1_500),
                                page_id=(
                                    str(session["js_reverse_page_id"])
                                    if session.get("js_reverse_page_id")
                                    else None
                                ),
                            )
                        except Exception as exc:
                            warnings.append(f"post-response alignment: {str(exc)[:1000]}")
                        if replay_plan.get("replay_mode") == "control" and (
                            replay_http_status is None
                            or replay_http_status < 200
                            or replay_http_status >= 300
                        ):
                            errors.append(
                                "Control replay did not produce a successful HTTP response."
                            )
                        step_results.append(
                            FlowStepResult(
                                step_id="replay_request",
                                status="completed",
                                started_at=started,
                                ended_at=utc_now(),
                            )
                        )
                    except asyncio.CancelledError:
                        step_results.append(
                            FlowStepResult(
                                step_id="replay_request",
                                status="canceled_outcome_unknown",
                                started_at=started,
                                ended_at=utc_now(),
                                error="Browser-context replay was canceled after dispatch.",
                            )
                        )
                        raise
                    except Exception as exc:
                        step_results.append(
                            FlowStepResult(
                                step_id="replay_request",
                                status="failed",
                                started_at=started,
                                ended_at=utc_now(),
                                error=str(exc)[:4000],
                            )
                        )
                        raise
                    for path, suffix, kind, sensitivity, credentials in [
                        (
                            spec_file,
                            "spec",
                            "replay_request_spec",
                            "credential",
                            True,
                        ),
                        (
                            diff_file,
                            "diff",
                            "replay_request_diff",
                            "private",
                            False,
                        ),
                        (
                            result_file,
                            "response",
                            "replay_response",
                            "private",
                            False,
                        ),
                    ]:
                        descriptor = self.experiments.describe_local_artifact(
                            str(path),
                            artifact_id=f"art_{experiment_id}_replay_{suffix}",
                            kind=kind,
                            sensitivity=sensitivity,
                            contains_credentials=credentials,
                        )
                        if descriptor:
                            replay_artifacts.append(descriptor)
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
                                    "matched_request_ids": result.get("matched_request_ids", []),
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
                        canceled_status = (
                            "canceled"
                            if step.action in {"wait", "assert", "snapshot"}
                            else "canceled_outcome_unknown"
                        )
                        step_results.append(
                            FlowStepResult(
                                step_id=step.step_id,
                                status=canceled_status,
                                started_at=started,
                                ended_at=utc_now(),
                                error=(
                                    "The read-only step was canceled."
                                    if canceled_status == "canceled"
                                    else (
                                        "The local command was canceled and its process tree "
                                        "was terminated. A side effect already delivered to "
                                        "the page cannot be rolled back generically."
                                    )
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
                            "matched_request_ids": wait_result.get("matched_request_ids", []),
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
                        stream_start_status=stream_start_status,
                        capture_transport_generation=capture_transport_generation,
                        trace_started=trace_started,
                        execution_deadline=deadline,
                        canceled=cancelled_error is not None,
                    ),
                    name=f"finalize-{experiment_id}",
                )
                try:
                    cleanup_result = await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cleanup_result = await cleanup_task
                stop_payload = dict(cleanup_result.get("stop_payload") or {})
                cleanup_status = dict(cleanup_result.get("final_status_payload") or {})
                if cleanup_status:
                    final_status_payload = cleanup_status
                trace_paths = list(cleanup_result.get("trace_paths") or [])
                screenshot_paths.extend(
                    str(item) for item in cleanup_result.get("screenshot_paths", [])
                )
                snapshot_paths.extend(
                    str(item) for item in cleanup_result.get("snapshot_paths", [])
                )
                network_payload = dict(cleanup_result.get("network_payload") or {})
                collector_stopped = bool(cleanup_result.get("collector_stopped"))
                warnings.extend(str(item) for item in cleanup_result.get("warnings", []))
                errors.extend(str(item) for item in cleanup_result.get("errors", []))

            post_alignment = AlignmentResult(
                status=(
                    "not_checked_due_to_cancel" if cancelled_error is not None else "not_checked"
                ),
                playwright_page=alignment.playwright_page,
                warnings=[
                    (
                        "Post-flow page alignment was not checked because the experiment "
                        "was canceled."
                        if cancelled_error is not None
                        else "Post-flow page alignment was not checked."
                    )
                ],
            )
            if cancelled_error is None:
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

            raw_network_requests = network_payload.get("requests")
            raw_network_requests = (
                [item for item in raw_network_requests if isinstance(item, dict)]
                if isinstance(raw_network_requests, list)
                else []
            )
            window_requests = requests_after_checkpoint(
                raw_network_requests,
                network_checkpoint_value,
                include_in_flight=payload.primary_request.include_in_flight,
            )
            network_payload["requests"] = window_requests
            network_payload["window"] = {
                **network_checkpoint_value,
                "matched_request_count": len(window_requests),
                "collector_generation_at_finalize": self._transport_generation(),
            }
            primary_network_payload = {
                **network_payload,
                "requests": [
                    item
                    for item in window_requests
                    if network_request_matches(item, request_matcher)
                ],
            }
            evidence_entries = self._evidence_index(manifest)
            evidence_artifacts: list[dict[str, Any]] = []
            if (
                cancelled_error is None
                and payload.network_evidence
                and self._transport_generation()
                == int(
                    network_checkpoint_value.get(
                        "collector_generation", self._transport_generation()
                    )
                )
            ):
                try:
                    (
                        exported_entries,
                        exported_artifacts,
                        export_warnings,
                    ) = await self._export_network_evidence(
                        experiment_id=experiment_id,
                        experiment_dir=experiment_dir,
                        selectors=payload.network_evidence,
                        requests=window_requests,
                        deadline=Deadline(8_000),
                        step_ids=[
                            item.step_id for item in step_results if item.status == "completed"
                        ],
                    )
                    evidence_entries.extend(exported_entries)
                    evidence_artifacts.extend(exported_artifacts)
                    warnings.extend(export_warnings)
                except Exception as exc:
                    warnings.append(f"network evidence export: {str(exc)[:3000]}")
            if cancelled_error is None and payload.capture.console_errors:
                (
                    console_entries,
                    console_artifacts,
                    console_warnings,
                ) = await self._export_console_evidence(
                    experiment_id=experiment_id,
                    experiment_dir=experiment_dir,
                    checkpoint=console_checkpoint_value,
                    deadline=Deadline(4_000),
                )
                evidence_entries.extend(console_entries)
                evidence_artifacts.extend(console_artifacts)
                warnings.extend(console_warnings)
            mutation_assessment: dict[str, Any] | None = None
            response_classification: dict[str, Any] | None = None
            stream_response_contract: dict[str, Any] | None = None
            response_evidence_source: str | None = None
            replay_network_evidence_id: str | None = None
            replay_inconclusive = False
            wire_snapshot: dict[str, Any] | None = None
            pre_dispatch_environment: dict[str, Any] | None = None
            post_response_environment: dict[str, Any] | None = None
            post_verification_environment: dict[str, Any] | None = None
            environment_comparison: dict[str, Any] | None = None
            if replay_plan is not None:
                replay_network_entry, replay_selection_error = self._select_replay_network_evidence(
                    evidence_entries,
                    replay_plan,
                )
                if replay_selection_error:
                    errors.append(replay_selection_error)
                replay_network_evidence_id = (
                    str(replay_network_entry.get("evidence_id"))
                    if isinstance(replay_network_entry, dict)
                    and replay_network_entry.get("evidence_id")
                    else None
                )
                wire_snapshot = self._network_evidence_snapshot(
                    self.experiments.root,
                    replay_network_entry,
                )
                associated_replay_streams: list[dict[str, Any]] = []
                if replay_network_evidence_id:
                    for stream_request in final_status_payload.get("requests", []):
                        if not isinstance(stream_request, dict):
                            continue
                        exact_evidence, _ = self._associate_stream_network_evidence(
                            stream_request,
                            [
                                item
                                for item in evidence_entries
                                if item.get("kind") == "network_request"
                            ],
                        )
                        if (
                            isinstance(exact_evidence, dict)
                            and exact_evidence.get("evidence_id") == replay_network_evidence_id
                        ):
                            associated_replay_streams.append(stream_request)
                if len(associated_replay_streams) == 1:
                    self._mark_snapshot_headers_complete_from_stream(
                        wire_snapshot,
                        associated_replay_streams[0],
                    )
                if isinstance(wire_snapshot, dict):
                    exact_status = wire_snapshot.get("status")
                    if isinstance(exact_status, int):
                        replay_http_status = exact_status
                    exact_content_type = response_content_type(wire_snapshot)
                    if exact_content_type:
                        replay_response_content_type = exact_content_type
                replay_plan["network_evidence_id"] = replay_network_evidence_id
                replay_manifest = manifest.get("replay")
                if isinstance(replay_manifest, dict):
                    replay_manifest["network_evidence_id"] = replay_network_evidence_id
                mutation = replay_plan.get("mutation")
                if replay_plan.get("replay_mode") == "treatment" and mutation is not None:
                    control_manifest = self.experiments.load_manifest(
                        str(replay_plan["control_experiment_id"])
                    )
                    control_replay = control_manifest.get("replay")
                    control_replay = control_replay if isinstance(control_replay, dict) else {}
                    control_network_evidence_id = control_replay.get("network_evidence_id")
                    control_entry = next(
                        (
                            item
                            for item in self._evidence_index(control_manifest)
                            if item.get("evidence_id") == control_network_evidence_id
                        ),
                        None,
                    )
                    control_snapshot = self._network_evidence_snapshot(
                        self.experiments.root,
                        control_entry,
                    )
                    mutation_assessment = assess_paired_mutation_effectiveness(
                        mutation,
                        control_snapshot,
                        wire_snapshot,
                        volatile_bindings=[
                            VolatileBinding.model_validate(item)
                            for item in replay_plan["volatile_bindings"]
                        ],
                        control_binding_values=replay_plan["control_volatile_binding_values"],
                        treatment_binding_values=replay_plan["current_volatile_binding_values"],
                        normalize_wire_order=bool(replay_plan.get("normalize_wire_order")),
                    )
                elif replay_plan.get("replay_mode") == "exploratory":
                    exploratory_assessments = [
                        assess_mutation_effectiveness(item, wire_snapshot)
                        for item in replay_plan.get("mutations", [])
                    ]
                    mutation_assessment = {
                        "mode": "exploratory",
                        "mutations": exploratory_assessments,
                        "all_mutations_effective": all(
                            item.get("mutation_effective") is True
                            for item in exploratory_assessments
                        )
                        if exploratory_assessments
                        else True,
                        "mutation_effective": None,
                        "reason": (
                            "exploratory mutations were evaluated independently"
                            if exploratory_assessments
                            else "exploratory replay requested no mutations"
                        ),
                    }
                else:
                    mutation_assessment = assess_control_wire_baseline(
                        wire_snapshot,
                        volatile_bindings=[
                            VolatileBinding.model_validate(item)
                            for item in replay_plan["volatile_bindings"]
                        ],
                        binding_values=replay_plan["current_volatile_binding_values"],
                    )
                    if mutation_assessment.get("volatile_bindings_effective") is not True:
                        errors.append(
                            "Control replay volatile bindings were not observed on the "
                            "outbound wire request."
                        )
                exact_response_value = response_value_from_snapshot(wire_snapshot)
                exact_replay_response_value = self._complete_replay_response_value(replay_response)
                response_value = (
                    exact_response_value
                    if exact_response_value is not None
                    else exact_replay_response_value
                    if exact_replay_response_value is not None
                    else replay_response
                )
                response_evidence_source = (
                    "exact_network_response_body"
                    if exact_response_value is not None
                    else "complete_replay_response_body"
                    if exact_replay_response_value is not None
                    else "replay_preview_fallback"
                )
                response_classification = classify_replay_response(
                    status=replay_http_status,
                    content_type=replay_response_content_type,
                    response_value=response_value,
                    mutation=mutation,
                    redirected=bool(self._extract_response_field(replay_response, "redirected")),
                    final_url=(
                        str(value)
                        if (
                            value := self._extract_response_field(
                                replay_response,
                                "url",
                            )
                        )
                        else None
                    ),
                    source_url=str(replay_plan["spec"].get("url", "")),
                    source_content_type=replay_plan.get("source_content_type"),
                )
                response_classification["evidence_source"] = response_evidence_source
                response_classification["evidence_sufficient"] = response_evidence_source in {
                    "exact_network_response_body",
                    "complete_replay_response_body",
                }
                observations = response_classification.get("observations")
                if isinstance(observations, dict) and isinstance(
                    mutation_assessment,
                    dict,
                ):
                    observations["mutation_effective"] = mutation_assessment.get(
                        "mutation_effective"
                    )
                stream_response_contract = self._stream_response_contract(
                    replay_plan,
                    replay_response,
                    status=replay_http_status,
                    content_type=replay_response_content_type,
                )
                if (
                    isinstance(stream_response_contract, dict)
                    and stream_response_contract.get("status") == "partial"
                ):
                    replay_inconclusive = True
                    warnings.append(
                        "Streaming response did not satisfy the configured terminal contract."
                    )
                if (
                    replay_plan.get("replay_mode") == "treatment"
                    and mutation_assessment.get("mutation_effective") is not True
                ):
                    errors.append(
                        "Requested treatment mutation was not observed on the "
                        "outbound wire request."
                    )
                classification = response_classification.get("classification")
                if replay_plan.get("replay_mode") == "control":
                    if classification != "success":
                        errors.append(
                            "Control replay response contract was not successful: "
                            f"{classification}."
                        )
                elif classification not in {
                    "success",
                    "validation_rejection",
                }:
                    if replay_plan.get("replay_mode") == "treatment":
                        replay_inconclusive = True
                        warnings.append(
                            "Treatment response is inconclusive for protocol inference: "
                            f"{classification}."
                        )
                environment_policy = replay_plan.get("environment_comparison")
                environment_policy = (
                    environment_policy if isinstance(environment_policy, dict) else {}
                )
                context_header_names = environment_policy.get("context_header_names")
                context_header_names = (
                    [str(item) for item in context_header_names]
                    if isinstance(context_header_names, list)
                    else None
                )
                pre_dispatch_environment = self._environment_fingerprint(
                    pre_dispatch_alignment,
                    wire_snapshot,
                    phase="pre_dispatch",
                    ignored_cookie_names=replay_plan.get("ignored_cookie_names"),
                    ignored_context_headers=replay_plan.get("ignored_context_headers"),
                    context_header_names=context_header_names,
                )
                post_response_environment = self._environment_fingerprint(
                    post_response_alignment,
                    None,
                    phase="post_response",
                    include_request_context=False,
                    ignored_cookie_names=replay_plan.get("ignored_cookie_names"),
                    ignored_context_headers=replay_plan.get("ignored_context_headers"),
                    context_header_names=context_header_names,
                )
                post_verification_environment = self._environment_fingerprint(
                    post_alignment,
                    None,
                    phase="post_verification",
                    include_request_context=False,
                    ignored_cookie_names=replay_plan.get("ignored_cookie_names"),
                    ignored_context_headers=replay_plan.get("ignored_context_headers"),
                    context_header_names=context_header_names,
                )
                if replay_plan.get("replay_mode") == "treatment":
                    control_manifest_for_environment = self.experiments.load_manifest(
                        str(replay_plan["control_experiment_id"])
                    )
                    environment_comparison = self._compare_pair_environments(
                        control_manifest_for_environment.get("pre_dispatch_environment"),
                        pre_dispatch_environment,
                        required_dimensions=environment_policy.get("required_dimensions"),
                        advisory_dimensions=environment_policy.get("advisory_dimensions"),
                    )
                    if environment_comparison.get("status") != "observed_equivalent":
                        warnings.append(
                            "Control/Treatment pre-dispatch environment is not fully "
                            f"equivalent: {environment_comparison.get('status')}."
                        )
            primary_network_payload["requests"] = [
                self._augment_network_request_with_evidence(item, evidence_entries)
                for item in primary_network_payload["requests"]
            ]
            stream_requests = final_status_payload.get("requests")
            if isinstance(stream_requests, list):
                for stream_request in stream_requests:
                    if not isinstance(stream_request, dict):
                        continue
                    exact_evidence, association = self._associate_stream_network_evidence(
                        stream_request,
                        [
                            item
                            for item in evidence_entries
                            if item.get("kind") == "network_request"
                        ],
                    )
                    if exact_evidence is None:
                        stream_request["networkEvidenceAssociation"] = association
                        continue
                    summary = exact_evidence.get("summary")
                    summary = summary if isinstance(summary, dict) else {}
                    snapshot_integrity = summary.get("snapshot_integrity")
                    snapshot_integrity = (
                        snapshot_integrity if isinstance(snapshot_integrity, dict) else {}
                    )
                    request_headers_completeness = str(
                        snapshot_integrity.get("request_headers_completeness") or "partial"
                    )
                    if self._stream_request_has_complete_request_headers(stream_request):
                        request_headers_completeness = "complete"
                    request_body_completeness = str(
                        snapshot_integrity.get("request_body_completeness") or "unknown"
                    )
                    network_snapshot_integrity = (
                        "complete"
                        if request_headers_completeness == "complete"
                        and request_body_completeness in {"complete", "not_required"}
                        else "partial"
                    )
                    stream_request["networkEvidenceId"] = exact_evidence.get("evidence_id")
                    stream_request["networkEvidenceAssociation"] = association
                    stream_request["networkSnapshotIntegrity"] = network_snapshot_integrity
                    stream_request["requestBodyCompleteness"] = request_body_completeness
                    stream_request["requestHeadersCompleteness"] = request_headers_completeness
                    stream_request["responseBodyCompleteness"] = snapshot_integrity.get(
                        "response_body_completeness"
                    )
                    stream_request["responseHeadersCompleteness"] = snapshot_integrity.get(
                        "response_headers_completeness"
                    )
            non_stream_error_response_observed = bool(
                replay_plan is not None
                and replay_plan.get("replay_mode") == "treatment"
                and replay_plan.get("source_is_stream") is True
                and isinstance(replay_http_status, int)
                and replay_http_status >= 400
                and replay_response_content_type != "text/event-stream"
                and replay_network_evidence_id
                and mutation_assessment is not None
                and mutation_assessment.get("mutation_effective") is True
            )
            protocol_rejection_observed = bool(
                non_stream_error_response_observed
                and response_classification is not None
                and isinstance(response_classification.get("observations"), dict)
                and response_classification["observations"].get("validation_like") is True
            )

            primary_status_payload = final_status_payload
            if (
                replay_plan is not None
                and replay_plan.get("source_is_stream") is True
                and replay_network_evidence_id
                and not non_stream_error_response_observed
            ):
                locked_stream_requests = [
                    item
                    for item in final_status_payload.get("requests", [])
                    if isinstance(item, dict)
                    and item.get("networkEvidenceId") == replay_network_evidence_id
                ]
                primary_status_payload = {
                    **final_status_payload,
                    "requests": locked_stream_requests,
                }
                if len(locked_stream_requests) != 1:
                    errors.append(
                        "Replay primary stream could not be locked to exactly one "
                        "networkRequestId + collectorGeneration association."
                    )

            (
                primary_requests,
                primary_integrity,
                count_ok,
                primary_dimensions,
            ) = self._primary_result(
                payload,
                primary_status_payload,
                primary_network_payload,
            )
            if non_stream_error_response_observed:
                primary_requests = list(primary_network_payload["requests"])
                count_ok = (
                    payload.primary_request.expected_min_matches
                    <= len(primary_requests)
                    <= payload.primary_request.expected_max_matches
                )
                replay_summary = (
                    replay_network_entry.get("summary")
                    if isinstance(replay_network_entry, dict)
                    and isinstance(replay_network_entry.get("summary"), dict)
                    else {}
                )
                snapshot_dimensions = replay_summary.get("snapshot_integrity")
                snapshot_dimensions = (
                    snapshot_dimensions if isinstance(snapshot_dimensions, dict) else {}
                )
                artifact_paths = (
                    replay_network_entry.get("artifact_paths")
                    if isinstance(replay_network_entry, dict)
                    and isinstance(replay_network_entry.get("artifact_paths"), dict)
                    else {}
                )
                request_snapshot_integrity = str(
                    snapshot_dimensions.get("network_snapshot_integrity") or "partial"
                )
                network_artifact_integrity = "complete" if artifact_paths.get("all") else "partial"
                response_body_completeness = str(
                    snapshot_dimensions.get("response_body_completeness") or "unknown"
                )
                if response_evidence_source == "complete_replay_response_body":
                    response_body_completeness = "complete"
                response_headers_completeness = str(
                    snapshot_dimensions.get("response_headers_completeness") or "partial"
                )
                primary_dimensions = {
                    "raw_capture": "not_applicable_non_stream_response",
                    "semantic_parse": "not_applicable_non_stream_response",
                    "request_snapshot": request_snapshot_integrity,
                    "artifacts": network_artifact_integrity,
                }
                applicable = [
                    request_snapshot_integrity,
                    network_artifact_integrity,
                    response_body_completeness,
                    response_headers_completeness,
                ]
                primary_integrity = (
                    max(applicable, key=self._integrity_severity) if count_ok else "failed"
                )
                if any(value != "complete" for value in applicable):
                    replay_inconclusive = True
                    warnings.append(
                        "Non-stream error response evidence is incomplete; field "
                        "classification is inconclusive."
                    )
            if payload.capture.stream:
                network_evidence_entries = [
                    item for item in evidence_entries if item.get("kind") == "network_request"
                ]
                evidence_entries.extend(
                    self._stream_evidence_entries(
                        experiment_id,
                        primary_requests,
                        network_evidence_entries,
                    )
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
                "not_required"
                if not payload.capture.stream
                else capture_summary.get("collectorIntegrity")
                or capture_summary.get("integrityStatus")
                or ("partial" if collector_started else "failed")
            )
            wait_met = wait_result is None or bool(wait_result.get("condition_met"))
            steps_ok = all(item.status == "completed" for item in step_results)
            execution_failed = (
                cancelled_error is not None
                or not steps_ok
                or not wait_met
                or (payload.capture.stream and not collector_stopped)
            )
            required_dimensions = {
                "raw_capture": (
                    payload.requirements.require_raw_capture
                    and not non_stream_error_response_observed
                ),
                "semantic_parse": (
                    payload.requirements.require_semantic_parse
                    and not non_stream_error_response_observed
                ),
                "request_snapshot": payload.requirements.require_request_snapshot,
                "artifacts": payload.requirements.require_artifacts,
            }
            required_values = [
                primary_dimensions[name]
                for name, required in required_dimensions.items()
                if required and payload.primary_request.expected_min_matches > 0
            ]
            evidence_failed = (
                bool(errors)
                or not count_ok
                or any(value == "failed" for value in required_values)
                or (
                    payload.capture.stream
                    and not payload.primary_request.allow_supporting_failures
                    and collector_integrity == "failed"
                )
            )
            evidence_partial = not evidence_failed and (
                replay_inconclusive
                or any(value != "complete" for value in required_values)
                or (
                    payload.capture.stream
                    and not payload.primary_request.allow_supporting_failures
                    and collector_integrity != "complete"
                )
            )
            execution_integrity = "failed" if execution_failed else "complete"
            evidence_integrity = (
                "failed" if evidence_failed else "partial" if evidence_partial else "complete"
            )
            causal_comparability = (
                str(environment_comparison.get("status") or "insufficient")
                if replay_plan is not None
                and replay_plan.get("replay_mode") == "treatment"
                and isinstance(environment_comparison, dict)
                else "insufficient"
                if replay_plan is not None and replay_plan.get("replay_mode") == "treatment"
                else "not_applicable"
            )
            if replay_plan is None or replay_plan.get("replay_mode") != "treatment":
                inference_eligibility = "ineligible"
            elif execution_integrity == "failed" or (
                isinstance(mutation_assessment, dict)
                and mutation_assessment.get("mutation_effective") is False
            ):
                inference_eligibility = "ineligible"
            elif (
                execution_integrity == "complete"
                and evidence_integrity == "complete"
                and causal_comparability == "observed_equivalent"
                and isinstance(mutation_assessment, dict)
                and mutation_assessment.get("mutation_effective") is True
            ):
                inference_eligibility = "eligible"
            else:
                inference_eligibility = "supervised_only"
            response_status = (
                "interrupted"
                if cancelled_error is not None
                else "failed"
                if "failed" in {execution_integrity, evidence_integrity}
                else "partial"
                if "partial" in {execution_integrity, evidence_integrity}
                else "completed"
            )
            pre_arm_request_count = sum(
                1 for item in primary_requests if bool(item.get("requestStartedBeforeCapture"))
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
                "stream_start_status": stream_start_status,
                "transport_generation": capture_transport_generation,
                "entered_finalize_reserve": cleanup_result.get("entered_finalize_reserve", False),
                "capture_scope": capture_summary.get("captureScope", "page-target-only"),
                "worker_coverage": capture_summary.get("workerCoverage", False),
            }
            artifacts = self._collect_artifacts(
                start_payload,
                final_status_payload,
                stop_payload,
                network_payload,
            )
            artifacts.extend(evidence_artifacts)
            artifacts.extend(replay_artifacts)
            for index, screenshot_path in enumerate(screenshot_paths, start=1):
                descriptor = self.experiments.describe_local_artifact(
                    screenshot_path,
                    artifact_id=f"art_{experiment_id}_screenshot_{index}",
                    kind="playwright_screenshot",
                    sensitivity="private",
                )
                if descriptor:
                    artifacts.append(descriptor)
                    evidence_entries.append(
                        {
                            "evidence_id": evidence_id(
                                experiment_id,
                                "page_screenshot",
                                stable_id=index,
                            ),
                            "kind": "page_screenshot",
                            "artifact_ids": [descriptor["artifactId"]],
                            "artifact_paths": {"screenshot": descriptor["relativePath"]},
                        }
                    )
            for index, snapshot_path in enumerate(snapshot_paths, start=1):
                descriptor = self.experiments.describe_local_artifact(
                    snapshot_path,
                    artifact_id=f"art_{experiment_id}_page_snapshot_{index}",
                    kind="playwright_page_snapshot",
                    sensitivity="private",
                )
                if descriptor:
                    artifacts.append(descriptor)
                    evidence_entries.append(
                        {
                            "evidence_id": evidence_id(
                                experiment_id,
                                "page_snapshot",
                                stable_id=index,
                            ),
                            "kind": "page_snapshot",
                            "artifact_ids": [descriptor["artifactId"]],
                            "artifact_paths": {"snapshot": descriptor["relativePath"]},
                        }
                    )
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
            relative_snapshot_paths = [
                relative
                for path in snapshot_paths
                if (relative := self.experiments.relative_path(path)) is not None
            ]
            relative_trace_paths = [
                relative
                for path in trace_paths
                if (relative := self.experiments.relative_path(path)) is not None
            ]
            if replay_plan is not None:
                replay_transport_semantics = {
                    **(
                        replay_plan.get("transport")
                        if isinstance(replay_plan.get("transport"), dict)
                        else {}
                    ),
                    "source_fetch_options_known": False,
                }
                replay_manifest = manifest.get("replay")
                if isinstance(replay_manifest, dict):
                    replay_manifest.update(
                        {
                            "network_evidence_id": replay_network_evidence_id,
                            "dispatch_wall_time_ms": replay_plan.get("dispatch_wall_time_ms"),
                            "replay_http_status": replay_http_status,
                            "response_classification": response_classification,
                            "mutation_assessment": mutation_assessment,
                            "stream_response_contract": stream_response_contract,
                            "response_evidence_source": response_evidence_source,
                            "pre_dispatch_environment": pre_dispatch_environment,
                            "post_response_environment": post_response_environment,
                            "post_verification_environment": (post_verification_environment),
                            "environment_comparison": environment_comparison,
                            "transport_semantics": replay_transport_semantics,
                        }
                    )
                replay_artifact_ids = [
                    str(item.get("artifactId"))
                    for item in replay_artifacts
                    if item.get("artifactId")
                ]
                evidence_entries.append(
                    {
                        "evidence_id": evidence_id(
                            experiment_id,
                            "replay_attempt",
                            stable_id=replay_plan["replay_attempt_id"],
                        ),
                        "kind": "replay_attempt",
                        "replay_attempt_id": replay_plan["replay_attempt_id"],
                        "pair_protocol_hash": replay_plan["pair_protocol_hash"],
                        "source_experiment_id": replay_plan["source_experiment_id"],
                        "source_evidence_id": replay_plan["source_evidence_id"],
                        "replay_mode": replay_plan["replay_mode"],
                        "control_experiment_id": replay_plan["control_experiment_id"],
                        "network_evidence_id": replay_network_evidence_id,
                        "mutation_assessment": mutation_assessment,
                        "response_classification": response_classification,
                        "stream_response_contract": stream_response_contract,
                        "response_evidence_source": response_evidence_source,
                        "pre_dispatch_environment": pre_dispatch_environment,
                        "post_response_environment": post_response_environment,
                        "post_verification_environment": (post_verification_environment),
                        "environment_comparison": environment_comparison,
                        "transport_semantics": replay_transport_semantics,
                        "artifact_ids": replay_artifact_ids,
                        "step_ids": ["replay_request"],
                        "summary": {
                            "http_status": replay_http_status,
                            "response_content_type": replay_response_content_type,
                            "protocol_rejection_observed": (protocol_rejection_observed),
                            "non_stream_error_response_observed": (
                                non_stream_error_response_observed
                            ),
                            "response_classification": (
                                response_classification.get("classification")
                                if isinstance(response_classification, dict)
                                else None
                            ),
                            "control_http_status": replay_plan.get("control_http_status"),
                            "http_status_differs_from_control": (
                                replay_http_status != replay_plan.get("control_http_status")
                                if replay_plan.get("replay_mode") == "treatment"
                                else None
                            ),
                            **{
                                key: replay_result.get(key)
                                for key in (
                                    "resultType",
                                    "filename",
                                    "byteLength",
                                    "charLength",
                                    "truncated",
                                )
                                if key in replay_result
                            },
                        },
                    }
                )
            manifest.update(
                {
                    "status": response_status,
                    "deadline": deadline.to_dict(),
                    "steps": [item.model_dump(mode="json") for item in step_results],
                    "stream_capture_id": capture_id,
                    "stream_status": final_status_payload,
                    "stream_runtime": {
                        "start_status": stream_start_status,
                        "capture_id": capture_id,
                        "capture_uuid": capture_uuid,
                        "capture_relative_dir": capture_relative_dir,
                        "capture_metadata_artifact_id": (capture_metadata_artifact_id),
                        "transport_generation": capture_transport_generation,
                        "capture_namespace": experiment_id,
                    },
                    "stream_wait_result": wait_result,
                    "wait_observations": wait_observations,
                    "collector_integrity": collector_integrity,
                    "primary_request_integrity": primary_integrity,
                    "execution_integrity": execution_integrity,
                    "evidence_integrity": evidence_integrity,
                    "causal_comparability": causal_comparability,
                    "inference_eligibility": inference_eligibility,
                    "objective_requirements": payload.requirements.model_dump(mode="json"),
                    "primary_integrity_dimensions": primary_dimensions,
                    "primary_requests": primary_requests,
                    "cancellation_classifications": cancellation_classifications,
                    "post_flow_alignment": asdict(post_alignment),
                    "capture_health": capture_health,
                    "network_checkpoint": network_checkpoint_value,
                    "network_summary": network_payload,
                    "console_checkpoint": console_checkpoint_value,
                    "screenshot_paths": relative_screenshot_paths,
                    "snapshot_paths": relative_snapshot_paths,
                    "trace_paths": relative_trace_paths,
                    "replay_result": replay_result,
                    "replay_http_status": replay_http_status,
                    "replay_response_content_type": replay_response_content_type,
                    "replay_response_classification": response_classification,
                    "stream_response_contract": stream_response_contract,
                    "response_evidence_source": response_evidence_source,
                    "replay_attempt_id": (
                        replay_plan["replay_attempt_id"] if replay_plan is not None else None
                    ),
                    "pair_protocol_hash": (
                        replay_plan["pair_protocol_hash"] if replay_plan is not None else None
                    ),
                    "pre_dispatch_environment": pre_dispatch_environment,
                    "post_response_environment": post_response_environment,
                    "post_verification_environment": post_verification_environment,
                    "pair_environment_comparison": environment_comparison,
                    "replay_transport_semantics": (
                        replay_transport_semantics if replay_plan is not None else None
                    ),
                    "protocol_rejection_observed": protocol_rejection_observed,
                    "non_stream_error_response_observed": (non_stream_error_response_observed),
                    "replay_comparison": (
                        {
                            "replay_mode": replay_plan["replay_mode"],
                            "control_experiment_id": replay_plan["control_experiment_id"],
                            "control_http_status": replay_plan.get("control_http_status"),
                            "treatment_http_status": replay_http_status,
                            "status_differs": (
                                replay_http_status != replay_plan.get("control_http_status")
                                if replay_plan["replay_mode"] == "treatment"
                                else None
                            ),
                            "response_classification": (
                                response_classification.get("classification")
                                if isinstance(response_classification, dict)
                                else None
                            ),
                            "pair_protocol_hash": replay_plan["pair_protocol_hash"],
                            "environment_comparison": environment_comparison,
                        }
                        if replay_plan is not None
                        else None
                    ),
                    "mutation_assessment": mutation_assessment,
                    "evidence": evidence_entries,
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
                    "manifest_relative_path": self._manifest_relative_path(experiment_id),
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
        owner = self.coordinator.browser_owner
        if owner is not None:
            await self._release_browser_operation(owner.owner_id)
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


def analysis_workspace_root_from_environment() -> Path:
    return (
        Path(
            env_value_from_environment_or_dotenv("WEB_REV_EVIDENCE_DIR")
            or "data/analysis-workspace"
        )
        .expanduser()
        .resolve()
    )


def build_browser_service_from_environment(
    *,
    evidence_root: Path | None = None,
    coordinator: RuntimeCoordinator | None = None,
) -> BrowserActionService:
    evidence_root = evidence_root or analysis_workspace_root_from_environment()
    experiments = ExperimentStore(evidence_root)
    browser_endpoint = env_value_from_environment_or_dotenv("WEB_REV_BROWSER_CDP_URL")
    playwright = PlaywrightCliAdapter(
        executable=(
            env_value_from_environment_or_dotenv("WEB_REV_PLAYWRIGHT_CLI") or "playwright-cli"
        ),
        cwd=experiments.root,
    )
    command = env_value_from_environment_or_dotenv("WEB_REV_JS_REVERSE_COMMAND") or "js-reverse-mcp"
    critical_args = [
        "--allowedRoots",
        str(experiments.root),
        "--streamArtifactRoot",
        "0",
    ]
    if browser_endpoint:
        critical_args[0:0] = ["--browserUrl", browser_endpoint]
    raw_args = env_value_from_environment_or_dotenv("WEB_REV_JS_REVERSE_EXTRA_ARGS")
    extra_args: list[str] = []
    if raw_args:
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            raise RuntimeError("WEB_REV_JS_REVERSE_EXTRA_ARGS must be a JSON array") from exc
        if not isinstance(parsed_args, list) or not all(
            isinstance(item, str) for item in parsed_args
        ):
            raise RuntimeError("WEB_REV_JS_REVERSE_EXTRA_ARGS must be a JSON string array")
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
        coordinator=coordinator,
    )
