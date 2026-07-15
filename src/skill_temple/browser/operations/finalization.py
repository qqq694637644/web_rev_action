"""Finalization responsibility extracted from BrowserActionService."""

# ruff: noqa: F403,F405,I001

from __future__ import annotations

from ._support import *  # noqa: F403

class BrowserFinalizationOperations:
    """Own finalization behavior while the public service remains a facade."""

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
