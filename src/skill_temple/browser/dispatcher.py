"""Public browser request dispatch separated from operation implementations."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from ..browser_models import (
    BrowserActionResponse,
    CancelExperimentRequest,
    CaptureBaselineRequest,
    CaptureFlowRequest,
    CloseSessionRequest,
    OpenSessionRequest,
    ReplayRequestRequest,
    RunBrowserExperimentRequest,
    SaveScriptSourceRequest,
)
from .core import BrowserServiceError, Deadline

if TYPE_CHECKING:
    from ..browser_service import BrowserActionService


async def dispatch_browser_request(
    service: BrowserActionService,
    request: RunBrowserExperimentRequest,
) -> BrowserActionResponse:
    """Route one public request to its specialized operation boundary."""
    if isinstance(request, CancelExperimentRequest):
        return await service._cancel_experiment(request)
    if isinstance(request, SaveScriptSourceRequest):
        return await service._save_script_source(request)
    if isinstance(request, OpenSessionRequest):
        session_id = request.payload.session_id or f"sess_{uuid.uuid4().hex[:12]}"
        owner_id = f"open_{uuid.uuid4().hex}"
        await service._reserve_browser_operation(
            session_id=session_id,
            owner_id=owner_id,
            operation="open_session",
        )
        try:
            return await service._open_session(request, session_id=session_id)
        finally:
            await service._release_browser_operation(owner_id)
    if isinstance(request, CloseSessionRequest):
        owner_id = f"close_{uuid.uuid4().hex}"
        await service._reserve_browser_operation(
            session_id=request.payload.session_id,
            owner_id=owner_id,
            operation="close_session",
        )
        try:
            return await service._close_session(request)
        finally:
            await service._release_browser_operation(owner_id)
    if isinstance(request, (CaptureFlowRequest, CaptureBaselineRequest, ReplayRequestRequest)):
        requested_operation: str | None = None
        replay_plan: dict[str, Any] | None = None
        if isinstance(request, ReplayRequestRequest):
            payload, replay_plan = service._prepare_replay_execution(request)
        else:
            request, requested_operation = service._normalize_capture_alias(request)
            payload = request.payload
        experiment_id = service.experiments.new_experiment_id()
        await service._reserve_browser_operation(
            session_id=payload.session_id,
            owner_id=experiment_id,
            operation=request.operation,
            experiment_id=experiment_id,
        )
        if payload.execution_mode == "job":
            try:
                response = service._start_capture_job(
                    request,
                    experiment_id=experiment_id,
                    payload=payload,
                    replay_plan=replay_plan,
                )
                if requested_operation is not None:
                    response.operation = requested_operation
                return response
            except Exception:
                await service._release_browser_operation(experiment_id)
                raise
        deadline = Deadline(payload.deadline_ms)
        try:
            experiment_id, experiment_dir, manifest = service.experiments.create_experiment(
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
            service._validate_and_store_series(
                session_id=payload.session_id,
                manifest=manifest,
                payload=payload,
            )
            if replay_plan:
                manifest["replay_source"] = {
                    "source_experiment_id": replay_plan["source_experiment_id"],
                    "source_evidence_id": replay_plan["source_evidence_id"],
                }
                manifest["replay"] = service._replay_manifest_seed(replay_plan)
            service.experiments.write_manifest(experiment_id, manifest)
            response = await service._capture_flow(
                request,
                deadline=deadline,
                prepared=(experiment_id, experiment_dir, manifest),
                payload=payload,
                replay_plan=replay_plan,
            )
            if requested_operation is not None:
                response.operation = requested_operation
            return response
        finally:
            await service._release_browser_operation(experiment_id)
    raise BrowserServiceError("unsupported_operation", "Unsupported browser operation", 400)
