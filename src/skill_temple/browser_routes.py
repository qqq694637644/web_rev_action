"""FastAPI registration for the two stable public Browser Actions."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .browser.transport import (
    BrowserTransportError,
    decode_inspect_envelope,
    decode_run_envelope,
)
from .browser_models import (
    BrowserActionResponse,
    InspectBrowserEvidenceEnvelope,
    RunBrowserExperimentEnvelope,
)
from .browser_service import BrowserActionService, BrowserServiceError
from .telemetry import TelemetryRecorder

_BROWSER_PATHS = {"/v1/browser/inspect", "/v1/browser/run"}


def _validation_path(loc: tuple[Any, ...]) -> str:
    parts = [str(item) for item in loc if item not in {"body"}]
    escaped = [part.replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped) if escaped else "/"


def _browser_error(
    *,
    code: str,
    operation: str,
    message: str,
    dispatch_started: bool,
    suggested_next_action: str,
    outcome: str | None = None,
    issues: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "operation": operation,
        "message": message,
        "dispatch_started": dispatch_started,
        "suggested_next_action": suggested_next_action,
    }
    if outcome is not None:
        error["outcome"] = outcome
    if issues:
        error["issues"] = issues
    return {"error": error}


def register_browser_actions(
    app: FastAPI,
    service: BrowserActionService,
    telemetry: TelemetryRecorder | None = None,
    protocol_skill_content_hash: str | None = None,
) -> None:
    app.state.browser_action_service = service

    @app.exception_handler(RequestValidationError)
    async def browser_request_validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if request.url.path not in _BROWSER_PATHS:
            return await request_validation_exception_handler(request, exc)
        body = exc.body if isinstance(exc.body, dict) else {}
        operation = str(body.get("operation") or "unknown")
        issues = [
            {
                "path": _validation_path(tuple(error.get("loc", ()))),
                "type": str(error.get("type", "validation_error")),
                "message": str(error.get("msg", "Invalid Browser envelope")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_browser_error(
                code="invalid_operation_payload",
                operation=operation,
                message="Browser Action envelope is invalid.",
                dispatch_started=False,
                issues=issues,
                suggested_next_action=(
                    "Read browser-action-protocol/docs/transport-envelope.md and submit "
                    "the complete six-field version-bound envelope."
                ),
            ),
        )

    @app.post(
        "/v1/browser/inspect",
        operation_id="inspectBrowserEvidence",
        response_model=BrowserActionResponse,
        response_model_exclude_none=True,
        summary="Inspect saved browser experiment evidence.",
        description="Decode one inspect operation from payload_json and return bounded evidence.",
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def inspect_browser_evidence(
        envelope: InspectBrowserEvidenceEnvelope,
    ) -> BrowserActionResponse | JSONResponse:
        if telemetry is not None:
            telemetry.record(
                "browser_request_received",
                action="inspect",
                operation=envelope.operation,
            )
        try:
            request = decode_inspect_envelope(
                envelope,
                skill_content_hash=protocol_skill_content_hash,
            )
        except BrowserTransportError as exc:
            if telemetry is not None:
                telemetry.record(
                    "browser_request_error",
                    action="inspect",
                    operation=envelope.operation,
                    code=exc.code,
                    dispatch_started=False,
                )
            return JSONResponse(status_code=exc.status_code, content=exc.response_content())
        if telemetry is not None:
            telemetry.record(
                "browser_request_valid",
                action="inspect",
                operation=envelope.operation,
            )
        try:
            response = await service.inspect(request)
            if telemetry is not None:
                telemetry.record(
                    "browser_request_completed",
                    action="inspect",
                    operation=envelope.operation,
                    status=response.status,
                )
            return response
        except BrowserServiceError as exc:
            if telemetry is not None:
                telemetry.record(
                    "browser_request_error",
                    action="inspect",
                    operation=envelope.operation,
                    code=exc.code,
                    dispatch_started=exc.dispatch_started,
                    outcome=exc.outcome,
                )
            return JSONResponse(
                status_code=exc.status_code,
                content=_browser_error(
                    code=exc.code,
                    operation=envelope.operation,
                    message=str(exc),
                    dispatch_started=exc.dispatch_started,
                    outcome=exc.outcome,
                    suggested_next_action=(
                        "Inspect the referenced session, experiment, or evidence handle."
                    ),
                ),
            )

    @app.post(
        "/v1/browser/run",
        operation_id="runBrowserExperiment",
        response_model=BrowserActionResponse,
        response_model_exclude_none=True,
        summary="Run one atomic browser experiment.",
        description=(
            "Decode one consequential operation from payload_json and execute it "
            "atomically."
        ),
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def run_browser_experiment(
        envelope: RunBrowserExperimentEnvelope,
    ) -> BrowserActionResponse | JSONResponse:
        if telemetry is not None:
            telemetry.record(
                "browser_request_received",
                action="run",
                operation=envelope.operation,
            )
        try:
            request = decode_run_envelope(
                envelope,
                skill_content_hash=protocol_skill_content_hash,
            )
        except BrowserTransportError as exc:
            if telemetry is not None:
                telemetry.record(
                    "browser_request_error",
                    action="run",
                    operation=envelope.operation,
                    code=exc.code,
                    dispatch_started=False,
                )
            return JSONResponse(status_code=exc.status_code, content=exc.response_content())
        if telemetry is not None:
            telemetry.record(
                "browser_request_valid",
                action="run",
                operation=envelope.operation,
            )
        try:
            response = await service.run(request)
            if telemetry is not None:
                telemetry.record(
                    "browser_request_completed",
                    action="run",
                    operation=envelope.operation,
                    status=response.status,
                )
            return response
        except BrowserServiceError as exc:
            if telemetry is not None:
                telemetry.record(
                    "browser_request_error",
                    action="run",
                    operation=envelope.operation,
                    code=exc.code,
                    dispatch_started=exc.dispatch_started,
                    outcome=exc.outcome,
                )
            return JSONResponse(
                status_code=exc.status_code,
                content=_browser_error(
                    code=exc.code,
                    operation=envelope.operation,
                    message=str(exc),
                    dispatch_started=exc.dispatch_started,
                    outcome=exc.outcome,
                    suggested_next_action=(
                        "Inspect the session or experiment before retrying when "
                        "dispatch_started is true."
                    ),
                ),
            )
        except (RuntimeError, OSError) as exc:
            if telemetry is not None:
                telemetry.record(
                    "browser_request_error",
                    action="run",
                    operation=envelope.operation,
                    code="operation_outcome_unknown",
                    dispatch_started=True,
                    outcome="unknown",
                )
            return JSONResponse(
                status_code=502,
                content=_browser_error(
                    code="operation_outcome_unknown",
                    operation=envelope.operation,
                    message=str(exc)[:4000],
                    dispatch_started=True,
                    outcome="unknown",
                    suggested_next_action=(
                        "Inspect the session or experiment terminal state; do not "
                        "repeat the run operation."
                    ),
                ),
            )
