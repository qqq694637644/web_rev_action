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
    CaptureBaselineRequest,
    CaptureFlowPayload,
    CaptureFlowRequest,
    CloseSessionRequest,
    FlowStepResult,
    OpenSessionRequest,
    RequestMatcher,
    WaitCondition,
)
from ...runtime_coordinator import RuntimeOwner, RuntimeReservationError
from ..adapters.contracts import (
    AlignmentResult,
    StreamCheckpoint,
    StreamRequestCheckpoint,
)
from ..core import BrowserServiceError, Deadline, _safe_identifier, utc_now
from ..stream_state import checkpoint_from_status, request_matches_stream_request


class BrowserSessionOperations:
    """Own session behavior while the public service remains a facade."""

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

    @staticmethod
    def _normalize_capture_alias(
        request: CaptureFlowRequest | CaptureBaselineRequest,
    ) -> tuple[CaptureFlowRequest, str | None]:
        if isinstance(request, CaptureFlowRequest):
            return request, None
        return (
            CaptureFlowRequest(
                contract_version=request.contract_version,
                operation="capture_flow",
                payload=request.payload,
                skill_binding=request.skill_binding,
            ),
            request.operation,
        )

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
        return checkpoint_from_status(status, matcher)

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
