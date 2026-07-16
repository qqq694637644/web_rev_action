"""Setup, action, and verification step execution."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..browser_models import FlowStep, FlowStepResult, RequestMatcher
from .adapters import AdapterError, StreamCheckpoint
from .core import BrowserServiceError, Deadline, service_error_from_adapter, utc_now

if TYPE_CHECKING:
    from ..browser_service import BrowserActionService

class StepExecutor:
    """Execute setup, action, and verification steps with one lifecycle."""

    READ_ONLY_ACTIONS = {"wait", "assert", "snapshot"}

    @classmethod
    async def execute_many(
        cls,
        service: BrowserActionService,
        *,
        phase: str,
        steps: list[FlowStep],
        session_id: str,
        experiment_dir: Path,
        deadline: Deadline,
        capture_id: int | None,
        request_matcher: RequestMatcher,
        stream_checkpoint: StreamCheckpoint,
        first_mutation_wall_time_ms: int | None,
        step_results: list[FlowStepResult],
        wait_observations: list[dict[str, Any]],
    ) -> tuple[StreamCheckpoint, int | None]:
        for step_index, step in enumerate(steps):
            label = f"{phase} step {step.step_id}"
            service._ensure_finalize_reserve(deadline, label)
            started = utc_now()
            try:
                if step.action not in cls.READ_ONLY_ACTIONS:
                    if capture_id is not None:
                        stream_checkpoint = await service._stream_checkpoint(
                            capture_id,
                            request_matcher,
                            service._operation_deadline(
                                deadline,
                                1_500,
                                f"checkpoint before {label}",
                            ),
                        )
                    if first_mutation_wall_time_ms is None:
                        first_mutation_wall_time_ms = int(time.time() * 1000)
                step_deadline = service._operation_deadline(
                    deadline,
                    step.timeout_ms,
                    label,
                )
                if step.action in {"wait", "assert"}:
                    result = await service._wait_condition(
                        session_ref=session_id,
                        capture_id=capture_id,
                        condition=step.condition,
                        checkpoint=stream_checkpoint,
                        deadline=step_deadline,
                    )
                    stream_checkpoint = service._checkpoint_from_wait_result(
                        result,
                    )
                    wait_observations.append(
                        {
                            "phase": phase,
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
                            f"{phase.capitalize()} condition failed: {step.step_id}",
                            409,
                        )
                    snapshot_ref = None
                else:
                    result = await service.playwright.execute_step(
                        session_id,
                        step,
                        experiment_dir,
                        step_deadline,
                    )
                    raw_snapshot_ref = result.get("snapshot_ref")
                    snapshot_ref = (
                        service.experiments.relative_path(str(raw_snapshot_ref))
                        if raw_snapshot_ref
                        else None
                    )
                step_results.append(
                    FlowStepResult(
                        step_id=step.step_id,
                        phase=phase,
                        status="completed",
                        started_at=started,
                        ended_at=utc_now(),
                        snapshot_ref=snapshot_ref,
                    )
                )
            except AdapterError as exc:
                service_error = service_error_from_adapter(
                    exc,
                    f"{phase} step {step.step_id}",
                    consequential=step.action not in cls.READ_ONLY_ACTIONS,
                )
                step_results.append(
                    FlowStepResult(
                        step_id=step.step_id,
                        phase=phase,
                        status=(
                            "outcome_unknown"
                            if service_error.code == "operation_outcome_unknown"
                            else "failed"
                        ),
                        started_at=started,
                        ended_at=utc_now(),
                        error=str(service_error)[:4000],
                    )
                )
                raise service_error from exc
            except asyncio.CancelledError:
                canceled_status = (
                    "canceled"
                    if step.action in cls.READ_ONLY_ACTIONS
                    else "canceled_outcome_unknown"
                )
                step_results.append(
                    FlowStepResult(
                        step_id=step.step_id,
                        phase=phase,
                        status=canceled_status,
                        started_at=started,
                        ended_at=utc_now(),
                        error=(
                            f"The {phase} read-only step was canceled."
                            if canceled_status == "canceled"
                            else (
                                f"The {phase} mutation step was canceled after dispatch; "
                                "page side effects cannot be rolled back generically."
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
                        phase=phase,
                        status="timed_out" if timed_out else "failed",
                        started_at=started,
                        ended_at=utc_now(),
                        error=str(exc)[:4000],
                    )
                )
                raise
        return stream_checkpoint, first_mutation_wall_time_ms
