"""Session reservation, alignment, waits, and request selection."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Any

from ...browser_models import (
    BrowserActionResponse,
    CancelExperimentRequest,
    CaptureFlowPayload,
    CloseSessionRequest,
    FlowStepResult,
    OpenSessionRequest,
    RequestMatcher,
    WaitCondition,
)
from ...protocol_evidence import public_alignment_summary, public_url_summary
from ...runtime_coordinator import RuntimeOwner, RuntimeReservationError
from ..adapters.contracts import (
    AdapterError,
    AlignmentResult,
    StreamCheckpoint,
    StreamRequestCheckpoint,
)
from ..core import (
    BrowserServiceError,
    Deadline,
    _safe_identifier,
    service_error_from_adapter,
    utc_now,
)
from ..session_states import NO_ATTACHMENT_STATES, REUSABLE_SESSION_STATES, TERMINAL_CLOSED
from ..stream_state import checkpoint_from_status, request_matches_stream_request


class BrowserSessionOperations:
    """Own session behavior while the public service remains a facade."""

    @staticmethod
    def _cancel_dispatch_started(exc: asyncio.CancelledError) -> bool:
        return bool(
            getattr(exc, "adapter_dispatch_started", False)
            or getattr(exc, "mcp_outcome_unknown", False)
        )

    @staticmethod
    def _record_open_stage_outcome(
        session: dict[str, Any],
        *,
        stage: str,
        outcome: str,
        attached: bool,
    ) -> None:
        if attached:
            session["attach_outcome"] = "confirmed"
        if stage == "attach":
            session["attach_outcome"] = outcome
        elif stage in {"navigation", "current_page", "page_selection"}:
            session["page_selection_outcome"] = outcome
        else:
            session["alignment_outcome"] = outcome

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
                try:
                    page = await self.playwright.current_page(
                        session_id, deadline.child(3_000)
                    )
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
                            dispatch_started=True,
                        )
                    return await callback(deadline)
                except AdapterError as exc:
                    raise service_error_from_adapter(
                        exc,
                        operation,
                        consequential=False,
                    ).with_context(session_id=session_id) from exc
        finally:
            await self._release_browser_operation(owner_id)

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
        if request.action_binding is not None:
            invocations = manifest.get("action_invocations")
            if not isinstance(invocations, list):
                invocations = []
                manifest["action_invocations"] = invocations
            invocations.append(
                {
                    **request.action_binding.model_dump(mode="json"),
                    "recorded_at": utc_now(),
                }
            )
            self.experiments.write_manifest(experiment_id, manifest)
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
            existing = self.sessions.get(session_id) or self.experiments.load_session(
                session_id
            )
            if existing and existing.get("status") not in REUSABLE_SESSION_STATES:
                raise BrowserServiceError(
                    "session_id_in_use",
                    f"Session ID is already in use with status {existing.get('status')!r}",
                    409,
                    dispatch_started=False,
                    session_id=session_id,
                )
            now = utc_now()
            session: dict[str, Any] = {
                "session_id": session_id,
                "status": "opening",
                "browser_endpoint_ref": endpoint,
                "playwright_session_ref": session_id,
                "evidence_store": "local",
                "evidence_root_ref": ".",
                "service_instance_id": self.service_instance_id,
                "process_started_at": self.process_started_at,
                "created_at": now,
                "updated_at": now,
                "attach_outcome": "pending",
                "page_selection_outcome": "not_started",
                "alignment_outcome": "not_started",
                "close_outcome": "not_started",
            }
            if request.action_binding is not None:
                session["action_contract"] = request.action_binding.model_dump(mode="json")
            self.sessions[session_id] = session
            self.experiments.save_session(session)
            page = None
            stage = "attach"
            try:
                page = await self.playwright.open_session(
                    session_id, endpoint, payload.target.start_url, deadline
                )
                session.update(
                    {
                        "status": "aligning",
                        "playwright_page_index": page.page_index,
                        "playwright_page_url": public_url_summary(page.url),
                        "playwright_page_title": page.title,
                        "attach_outcome": "confirmed",
                        "page_selection_outcome": "confirmed",
                        "updated_at": utc_now(),
                    }
                )
                self.experiments.save_session(session)
                if (
                    payload.target.page_index is not None
                    and payload.target.page_index != page.page_index
                ):
                    stage = "page_selection"
                    session["page_selection_outcome"] = "pending"
                    session["updated_at"] = utc_now()
                    self.experiments.save_session(session)
                    page = await self.playwright.select_page(
                        session_id,
                        payload.target.page_index,
                        deadline,
                    )
                    session.update(
                        {
                            "playwright_page_index": page.page_index,
                            "playwright_page_url": public_url_summary(page.url),
                            "playwright_page_title": page.title,
                            "page_selection_outcome": "confirmed",
                            "updated_at": utc_now(),
                        }
                    )
                    self.experiments.save_session(session)
                stage = "alignment"
                session["alignment_outcome"] = "pending"
                session["updated_at"] = utc_now()
                self.experiments.save_session(session)
                alignment = await self.js_reverse.align_page(page, deadline)
            except asyncio.CancelledError as exc:
                failure_stage = str(getattr(exc, "playwright_stage", stage))
                attached = bool(getattr(exc, "session_attached", page is not None))
                dispatch_started = self._cancel_dispatch_started(exc)
                outcome = "unknown" if dispatch_started else "canceled"
                self._record_open_stage_outcome(
                    session,
                    stage=failure_stage,
                    outcome=outcome,
                    attached=attached,
                )
                session.update(
                    {
                        "status": (
                            "open_unaligned"
                            if attached
                            else (
                                "open_outcome_unknown"
                                if dispatch_started
                                else "open_canceled_before_dispatch"
                            )
                        ),
                        "open_error": {
                            "code": "operation_canceled",
                            "message": (
                                "Open session was canceled after adapter dispatch."
                                if dispatch_started
                                else "Open session was canceled before adapter dispatch."
                            ),
                            "dispatch_started": dispatch_started,
                            "outcome": "unknown" if dispatch_started else "canceled",
                            "stage": failure_stage,
                        },
                        "updated_at": utc_now(),
                    }
                )
                self.experiments.save_session(session)
                raise
            except AdapterError as exc:
                failure_stage = str(getattr(exc, "playwright_stage", stage))
                attached = bool(getattr(exc, "session_attached", page is not None))
                service_error = service_error_from_adapter(
                    exc,
                    "open browser session",
                    consequential=True,
                ).with_context(session_id=session_id)
                unknown = service_error.code == "operation_outcome_unknown"
                self._record_open_stage_outcome(
                    session,
                    stage=failure_stage,
                    outcome="unknown" if unknown else "failed",
                    attached=attached,
                )
                session.update(
                    {
                        "status": (
                            "open_unaligned"
                            if attached
                            else (
                                "open_outcome_unknown"
                                if unknown
                                else (
                                    "open_failed"
                                    if service_error.dispatch_started
                                    else "open_failed_before_dispatch"
                                )
                            )
                        ),
                        "open_error": {
                            "code": service_error.code,
                            "message": str(service_error),
                            "dispatch_started": service_error.dispatch_started,
                            "outcome": service_error.outcome,
                            "stage": failure_stage,
                        },
                        "updated_at": utc_now(),
                    }
                )
                self.experiments.save_session(session)
                raise service_error from exc
            if alignment.status != "aligned":
                session.update(
                    {
                        "status": "alignment_failed",
                        "alignment_outcome": "failed",
                        "page_alignment_status": alignment.status,
                        "alignment_warnings": alignment.warnings,
                        "updated_at": utc_now(),
                    }
                )
                self.experiments.save_session(session)
                try:
                    session["close_outcome"] = "pending"
                    session["updated_at"] = utc_now()
                    self.experiments.save_session(session)
                    await self.playwright.close_session(session_id, deadline)
                except asyncio.CancelledError as exc:
                    dispatch_started = self._cancel_dispatch_started(exc)
                    session.update(
                        {
                            "status": (
                                "close_outcome_unknown"
                                if dispatch_started
                                else "alignment_failed"
                            ),
                            "close_outcome": (
                                "unknown" if dispatch_started else "canceled_before_dispatch"
                            ),
                            "close_error": {
                                "code": "operation_canceled",
                                "message": (
                                    "Alignment cleanup close was canceled after dispatch."
                                    if dispatch_started
                                    else "Alignment cleanup close was canceled before dispatch."
                                ),
                                "dispatch_started": dispatch_started,
                                "outcome": "unknown" if dispatch_started else "canceled",
                            },
                            "updated_at": utc_now(),
                        }
                    )
                    self.experiments.save_session(session)
                    raise
                except AdapterError as exc:
                    service_error = service_error_from_adapter(
                        exc,
                        "close browser session after alignment failure",
                        consequential=True,
                    ).with_context(session_id=session_id)
                    session.update(
                        {
                            "status": (
                                "close_outcome_unknown"
                                if service_error.code == "operation_outcome_unknown"
                                else "close_failed"
                            ),
                            "close_error": {
                                "code": service_error.code,
                                "message": str(service_error),
                                "dispatch_started": service_error.dispatch_started,
                                "outcome": service_error.outcome,
                            },
                            "close_outcome": (
                                "unknown"
                                if service_error.code == "operation_outcome_unknown"
                                else "failed"
                            ),
                            "updated_at": utc_now(),
                        }
                    )
                    self.experiments.save_session(session)
                    raise service_error from exc
                session["status"] = "closed_after_alignment_failure"
                session["close_outcome"] = "confirmed"
                session["updated_at"] = utc_now()
                self.experiments.save_session(session)
                raise BrowserServiceError(
                    "page_alignment_failed",
                    "; ".join(alignment.warnings) or "Could not align browser page",
                    409,
                    dispatch_started=True,
                    outcome="failed",
                    session_id=session_id,
                )
            session.update(
                {
                    "status": "open",
                    "playwright_page_index": page.page_index,
                    "playwright_page_url": public_url_summary(page.url),
                    "playwright_page_title": page.title,
                    "js_reverse_page_index": alignment.js_reverse_page_index,
                    "js_reverse_page_id": alignment.js_reverse_page_id,
                    "js_reverse_page_url": public_url_summary(
                        alignment.js_reverse_page_url
                    ),
                    "page_alignment_status": alignment.status,
                    "alignment_outcome": "confirmed",
                    "updated_at": utc_now(),
                }
            )
            self.experiments.save_session(session)
        return BrowserActionResponse(
            operation=request.operation,
            status="completed",
            session_id=session_id,
            result={
                "session": session,
                "alignment": public_alignment_summary(alignment),
            },
            warnings=alignment.warnings,
        )

    async def _close_session(self, request: CloseSessionRequest) -> BrowserActionResponse:
        deadline = Deadline(request.payload.deadline_ms)
        session_id = request.payload.session_id
        async with self._locked_browser_session(session_id, deadline):
            session = self._get_session(session_id)
            if session.get("status") in NO_ATTACHMENT_STATES:
                session["status"] = "closed"
                session["close_outcome"] = "not_required"
                session["updated_at"] = utc_now()
            elif session.get("status") not in TERMINAL_CLOSED:
                previous_status = str(session.get("status") or "unknown")
                try:
                    session["close_outcome"] = "pending"
                    session["updated_at"] = utc_now()
                    self.experiments.save_session(session)
                    await self.playwright.close_session(session_id, deadline)
                except asyncio.CancelledError as exc:
                    dispatch_started = self._cancel_dispatch_started(exc)
                    session.update(
                        {
                            "status": (
                                "close_outcome_unknown"
                                if dispatch_started
                                else previous_status
                            ),
                            "previous_status": previous_status,
                            "close_outcome": (
                                "unknown" if dispatch_started else "canceled_before_dispatch"
                            ),
                            "close_error": {
                                "code": "operation_canceled",
                                "message": (
                                    "Close session was canceled after adapter dispatch."
                                    if dispatch_started
                                    else "Close session was canceled before adapter dispatch."
                                ),
                                "dispatch_started": dispatch_started,
                                "outcome": "unknown" if dispatch_started else "canceled",
                            },
                            "updated_at": utc_now(),
                        }
                    )
                    if request.action_binding is not None:
                        session["last_action_contract"] = request.action_binding.model_dump(
                            mode="json"
                        )
                    self.experiments.save_session(session)
                    raise
                except AdapterError as exc:
                    service_error = service_error_from_adapter(
                        exc,
                        "close browser session",
                        consequential=True,
                    ).with_context(session_id=session_id)
                    session.update(
                        {
                            "status": (
                                "close_outcome_unknown"
                                if service_error.code == "operation_outcome_unknown"
                                else "close_failed"
                            ),
                            "close_error": {
                                "code": service_error.code,
                                "message": str(service_error),
                                "dispatch_started": service_error.dispatch_started,
                                "outcome": service_error.outcome,
                            },
                            "close_outcome": (
                                "unknown"
                                if service_error.code == "operation_outcome_unknown"
                                else "failed"
                            ),
                            "updated_at": utc_now(),
                        }
                    )
                    if request.action_binding is not None:
                        session["last_action_contract"] = request.action_binding.model_dump(
                            mode="json"
                        )
                    self.experiments.save_session(session)
                    raise service_error from exc
            if session.get("status") not in TERMINAL_CLOSED:
                session["status"] = "closed"
                if session.get("close_outcome") != "not_required":
                    session["close_outcome"] = "confirmed"
            session["updated_at"] = utc_now()
            if request.action_binding is not None:
                session["last_action_contract"] = request.action_binding.model_dump(mode="json")
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
                "playwright_page_url": public_url_summary(page.url),
                "playwright_page_title": page.title,
                "playwright_page_index": page.page_index,
                "js_reverse_page_index": alignment.js_reverse_page_index,
                "js_reverse_page_id": alignment.js_reverse_page_id,
                "js_reverse_page_url": public_url_summary(
                    alignment.js_reverse_page_url
                ),
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
        result = await self.playwright.wait_for_page_condition(
            session_ref,
            condition,
            condition_deadline,
        )
        if condition.type == "page_url" and isinstance(result.get("url"), str):
            result["url"] = public_url_summary(result["url"])
        return result

    async def _stream_checkpoint(
        self,
        capture_id: int | None,
        matcher: RequestMatcher,
        deadline: Deadline,
    ) -> StreamCheckpoint:
        if capture_id is None:
            return StreamCheckpoint()
        status = await self.js_reverse.get_stream_status(capture_id, deadline)
        return checkpoint_from_status(status, matcher)

    @staticmethod
    def _checkpoint_from_wait_result(
        result: dict[str, Any],
    ) -> StreamCheckpoint:
        def invalid(path: str) -> BrowserServiceError:
            return BrowserServiceError(
                "invalid_adapter_response",
                f"Invalid adapter response at {path}",
                502,
                dispatch_started=True,
            )

        value = result.get("checkpoint")
        if not isinstance(value, dict):
            raise invalid("/checkpoint")
        if set(value) != {"version", "requests"}:
            raise invalid("/checkpoint")
        version = value.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise invalid("/checkpoint/version")
        requests_value = value.get("requests")
        if not isinstance(requests_value, dict):
            raise invalid("/checkpoint/requests")
        requests: dict[str, StreamRequestCheckpoint] = {}
        expected_fields = {
            "response_observed",
            "status",
            "terminal_wall_time_ms",
            "raw_event_index",
            "semantic_event_index",
            "primary_event_source",
        }
        for request_id, request_value in requests_value.items():
            request_path = f"/checkpoint/requests/{request_id}"
            if not isinstance(request_id, str) or not request_id:
                raise invalid("/checkpoint/requests")
            if not isinstance(request_value, dict) or set(request_value) != expected_fields:
                raise invalid(request_path)
            response_observed = request_value["response_observed"]
            status = request_value["status"]
            terminal_wall_time_ms = request_value["terminal_wall_time_ms"]
            raw_event_index = request_value["raw_event_index"]
            semantic_event_index = request_value["semantic_event_index"]
            primary_event_source = request_value["primary_event_source"]
            if not isinstance(response_observed, bool):
                raise invalid(f"{request_path}/response_observed")
            if status is not None and not isinstance(status, str):
                raise invalid(f"{request_path}/status")
            if terminal_wall_time_ms is not None and (
                not isinstance(terminal_wall_time_ms, (int, float))
                or isinstance(terminal_wall_time_ms, bool)
            ):
                raise invalid(f"{request_path}/terminal_wall_time_ms")
            if not isinstance(raw_event_index, int) or isinstance(raw_event_index, bool):
                raise invalid(f"{request_path}/raw_event_index")
            if not isinstance(semantic_event_index, int) or isinstance(
                semantic_event_index, bool
            ):
                raise invalid(f"{request_path}/semantic_event_index")
            if not isinstance(primary_event_source, str):
                raise invalid(f"{request_path}/primary_event_source")
            requests[request_id] = StreamRequestCheckpoint(
                response_observed=response_observed,
                status=status,
                terminal_wall_time_ms=(
                    float(terminal_wall_time_ms)
                    if terminal_wall_time_ms is not None
                    else None
                ),
                raw_event_index=raw_event_index,
                semantic_event_index=semantic_event_index,
                primary_event_source=primary_event_source,
            )
        return StreamCheckpoint(version=version, requests=requests)

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
    def _select_primary_requests(
        payload: CaptureFlowPayload,
        status_payload: dict[str, Any],
        network_payload: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], bool]:
        matcher = BrowserSessionOperations._request_matcher(payload)
        requests = [
            item
            for item in status_payload.get("requests", [])
            if isinstance(item, dict)
            and request_matches_stream_request(item, matcher)
        ]
        if not requests and not payload.capture.stream:
            requests = [
                dict(item)
                for item in network_payload.get("requests", [])
                if isinstance(item, dict)
                and request_matches_stream_request(item, matcher)
            ]
        count_ok = (
            payload.primary_request.expected_min_matches
            <= len(requests)
            <= payload.primary_request.expected_max_matches
        )
        return requests, count_ok

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
