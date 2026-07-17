"""Atomic browser experiment orchestration and workspace evidence storage."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from .browser.adapters.contracts import (
    JsReverseAdapter,
    McpToolTransport,
    PlaywrightAdapter,
)
from .browser.adapters.js_reverse import JsReverseMcpAdapter
from .browser.adapters.mcp import StdioMcpToolTransport
from .browser.adapters.playwright import PlaywrightCliAdapter
from .browser.artifacts import ExperimentStore
from .browser.core import BrowserServiceError, Deadline, utc_now
from .browser.dispatcher import dispatch_browser_request
from .browser.operations.capture import BrowserCaptureOperations
from .browser.operations.evidence import BrowserEvidenceOperations
from .browser.operations.finalization import BrowserFinalizationOperations
from .browser.operations.inspection import BrowserInspectionOperations
from .browser.operations.replay import BrowserReplayOperations
from .browser.operations.replay_analysis import BrowserReplayAnalysisOperations
from .browser.operations.session import BrowserSessionOperations
from .browser.session_states import MAY_HOLD_ATTACHMENT
from .browser_models import (
    BrowserActionResponse,
    RunBrowserExperimentRequest,
)
from .runtime import env_value_from_environment_or_dotenv
from .runtime_coordinator import (
    RuntimeCoordinator,
)

__all__ = [
    "BrowserActionService",
    "BrowserServiceError",
    "Deadline",
    "ExperimentStore",
    "analysis_workspace_root_from_environment",
    "build_browser_service_from_environment",
]


class BrowserActionService(
    BrowserCaptureOperations,
    BrowserFinalizationOperations,
    BrowserReplayOperations,
    BrowserReplayAnalysisOperations,
    BrowserEvidenceOperations,
    BrowserInspectionOperations,
    BrowserSessionOperations,
):
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


    async def run(self, request: RunBrowserExperimentRequest) -> BrowserActionResponse:
        return await dispatch_browser_request(self, request)


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
                session.get("status") not in MAY_HOLD_ATTACHMENT
                or session.get("service_instance_id") != self.service_instance_id
            ):
                continue
            deadline = Deadline(5_000)
            try:
                async with self._locked_browser_session(session_id, deadline):
                    await self.playwright.close_session(session_id, deadline)
                    session["status"] = "closed"
                    session["close_outcome"] = "confirmed"
                    session["close_reason"] = "service_shutdown"
                    session["updated_at"] = utc_now()
                    self.experiments.save_session(session)
            except Exception:
                session["previous_status"] = session.get("status")
                session["status"] = "stale"
                session["stale_reason"] = "shutdown_detach_failed"
                session["close_outcome"] = "unknown"
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
