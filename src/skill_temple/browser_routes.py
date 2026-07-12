"""FastAPI registration for the two public browser Actions."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .browser_models import (
    BrowserActionResponse,
    InspectBrowserEvidenceRequest,
    RunBrowserExperimentRequest,
)
from .browser_service import BrowserActionService, BrowserServiceError


def register_browser_actions(app: FastAPI, service: BrowserActionService) -> None:
    app.state.browser_action_service = service

    @app.post(
        "/v1/browser/inspect",
        operation_id="inspectBrowserEvidence",
        response_model=BrowserActionResponse,
        response_model_exclude_none=True,
        summary="Inspect saved browser experiment evidence.",
        description=(
            "Read sessions, experiment manifests, stream status, and bounded artifacts. "
            "Credential artifacts are redacted unless full local replay access is explicit."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def inspect_browser_evidence(
        request: InspectBrowserEvidenceRequest,
    ) -> BrowserActionResponse:
        try:
            return await service.inspect(request)
        except BrowserServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "suggested_next_action": "inspect_request_or_configuration",
                    }
                },
            ) from exc

    @app.post(
        "/v1/browser/run",
        operation_id="runBrowserExperiment",
        response_model=BrowserActionResponse,
        response_model_exclude_none=True,
        summary="Run one atomic browser experiment.",
        description=(
            "Open or close a session, capture a baseline, or atomically execute page actions "
            "between private stream start/wait/stop calls and write one experiment manifest."
        ),
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def run_browser_experiment(
        request: RunBrowserExperimentRequest,
    ) -> BrowserActionResponse:
        try:
            return await service.run(request)
        except BrowserServiceError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "suggested_next_action": "inspect_session_or_experiment",
                    }
                },
            ) from exc
        except (RuntimeError, OSError) as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": {
                        "code": "browser_backend_error",
                        "message": str(exc)[:4000],
                        "suggested_next_action": "check_private_adapter_configuration",
                    }
                },
            ) from exc
