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

_BROWSER_PATHS = {"/v1/browser/inspect", "/v1/browser/run"}
_SKILL_PATHS = {"/v1/skills/load", "/v1/skills/read"}


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
    session_id: str | None = None,
    experiment_id: str | None = None,
    manifest_relative_path: str | None = None,
    adapter_error_code: str | None = None,
    retryable: bool | None = None,
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
    if session_id is not None:
        error["session_id"] = session_id
    if experiment_id is not None:
        error["experiment_id"] = experiment_id
    if manifest_relative_path is not None:
        error["manifest_relative_path"] = manifest_relative_path
    if adapter_error_code is not None:
        error["adapter_error_code"] = adapter_error_code
    if retryable is not None:
        error["retryable"] = retryable
    return {"error": error}


def register_browser_actions(
    app: FastAPI,
    service: BrowserActionService,
    protocol_skill_content_hash: str | None = None,
) -> None:
    app.state.browser_action_service = service

    @app.exception_handler(RequestValidationError)
    async def browser_request_validation_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if request.url.path in _SKILL_PATHS:
            details = "; ".join(
                f"{_validation_path(tuple(error.get('loc', ())))}: "
                f"{error.get('msg', 'invalid value')}"
                for error in exc.errors()[:10]
            )
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_skill_request",
                        "message": f"Skill Action request is invalid. {details}".strip(),
                        "suggested_next_action": "check_request_fields",
                    }
                },
            )
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
        try:
            request = decode_inspect_envelope(
                envelope,
                skill_content_hash=protocol_skill_content_hash,
            )
        except BrowserTransportError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.response_content())
        try:
            response = await service.inspect(request)
            return response
        except BrowserServiceError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=_browser_error(
                    code=exc.code,
                    operation=envelope.operation,
                    message=str(exc),
                    dispatch_started=exc.dispatch_started,
                    outcome=exc.outcome,
                    session_id=exc.session_id,
                    experiment_id=exc.experiment_id,
                    manifest_relative_path=exc.manifest_relative_path,
                    adapter_error_code=exc.adapter_error_code,
                    retryable=exc.retryable,
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
        try:
            request = decode_run_envelope(
                envelope,
                skill_content_hash=protocol_skill_content_hash,
            )
        except BrowserTransportError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.response_content())
        try:
            response = await service.run(request)
            return response
        except BrowserServiceError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=_browser_error(
                    code=exc.code,
                    operation=envelope.operation,
                    message=str(exc),
                    dispatch_started=exc.dispatch_started,
                    outcome=exc.outcome,
                    session_id=exc.session_id,
                    experiment_id=exc.experiment_id,
                    manifest_relative_path=exc.manifest_relative_path,
                    adapter_error_code=exc.adapter_error_code,
                    retryable=exc.retryable,
                    suggested_next_action=(
                        "Inspect the session or experiment; do not repeat the operation "
                        "while dispatch_started is true and the outcome is unresolved."
                    ),
                ),
            )
