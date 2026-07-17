"""Registry-driven dispatch for consequential Browser operations."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from ..browser_models import (
    BrowserActionResponse,
    CancelExperimentRequest,
    CaptureFlowPayload,
    CaptureFlowRequest,
    CloseSessionRequest,
    OpenSessionRequest,
    ReplayRequestRequest,
    RunBrowserExperimentRequest,
    SaveScriptSourceRequest,
)
from .core import BrowserServiceError, Deadline
from .registry import OPERATION_REGISTRY

if TYPE_CHECKING:
    from ..browser_service import BrowserActionService

RunHandler = Callable[
    ["BrowserActionService", RunBrowserExperimentRequest],
    Awaitable[BrowserActionResponse],
]


def _binding_dict(request: RunBrowserExperimentRequest) -> dict[str, str] | None:
    binding = request.action_binding
    if binding is None:
        return None
    return binding.model_dump(mode="json")


async def dispatch_browser_request(
    service: BrowserActionService,
    request: RunBrowserExperimentRequest,
) -> BrowserActionResponse:
    """Route through the handler declared by the authoritative operation registry."""

    spec = OPERATION_REGISTRY.require(request.operation)
    if spec.action != "run":
        raise BrowserServiceError("unsupported_operation", "Unsupported run operation", 400)
    handler = globals().get(spec.handler_name)
    if not callable(handler):
        raise RuntimeError(
            f"Operation registry handler is unavailable: {spec.name} -> {spec.handler_name}"
        )
    return await cast(RunHandler, handler)(service, request)


async def dispatch_cancel_experiment(
    service: BrowserActionService,
    request: RunBrowserExperimentRequest,
) -> BrowserActionResponse:
    return await service._cancel_experiment(cast(CancelExperimentRequest, request))


async def dispatch_save_script_source(
    service: BrowserActionService,
    request: RunBrowserExperimentRequest,
) -> BrowserActionResponse:
    return await service._save_script_source(cast(SaveScriptSourceRequest, request))


async def dispatch_open_session(
    service: BrowserActionService,
    request: RunBrowserExperimentRequest,
) -> BrowserActionResponse:
    typed = cast(OpenSessionRequest, request)
    session_id = typed.payload.session_id or f"sess_{uuid.uuid4().hex[:12]}"
    owner_id = f"open_{uuid.uuid4().hex}"
    await service._reserve_browser_operation(
        session_id=session_id,
        owner_id=owner_id,
        operation=typed.operation,
    )
    try:
        return await service._open_session(typed, session_id=session_id)
    finally:
        await service._release_browser_operation(owner_id)


async def dispatch_close_session(
    service: BrowserActionService,
    request: RunBrowserExperimentRequest,
) -> BrowserActionResponse:
    typed = cast(CloseSessionRequest, request)
    owner_id = f"close_{uuid.uuid4().hex}"
    await service._reserve_browser_operation(
        session_id=typed.payload.session_id,
        owner_id=owner_id,
        operation=typed.operation,
    )
    try:
        return await service._close_session(typed)
    finally:
        await service._release_browser_operation(owner_id)


async def _dispatch_capture_like(
    service: BrowserActionService,
    request: CaptureFlowRequest | ReplayRequestRequest,
) -> BrowserActionResponse:
    replay_plan: dict[str, Any] | None = None
    payload: CaptureFlowPayload
    if isinstance(request, ReplayRequestRequest):
        payload, replay_plan = service._prepare_replay_execution(request)
    else:
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
            return service._start_capture_job(
                request,
                experiment_id=experiment_id,
                payload=payload,
                replay_plan=replay_plan,
            )
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
            action_binding=_binding_dict(request),
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
        return await service._capture_flow(
            request,
            deadline=deadline,
            prepared=(experiment_id, experiment_dir, manifest),
            payload=payload,
            replay_plan=replay_plan,
        )
    finally:
        await service._release_browser_operation(experiment_id)


async def dispatch_capture_flow(
    service: BrowserActionService,
    request: RunBrowserExperimentRequest,
) -> BrowserActionResponse:
    return await _dispatch_capture_like(service, cast(CaptureFlowRequest, request))


async def dispatch_replay_request(
    service: BrowserActionService,
    request: RunBrowserExperimentRequest,
) -> BrowserActionResponse:
    return await _dispatch_capture_like(service, cast(ReplayRequestRequest, request))
