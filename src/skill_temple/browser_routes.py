"""FastAPI registration for the two public browser Actions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import FastAPI, HTTPException

from .browser_models import (
    BrowserActionResponse,
    InspectBrowserEvidenceRequest,
    RunBrowserExperimentRequest,
)
from .browser_service import BrowserActionService, BrowserServiceError


def _request_object_schema(
    openapi_schema: dict[str, Any],
    generated_request_schema: dict[str, Any],
) -> dict[str, object]:
    """Convert a generated discriminated union into an object request envelope."""

    discriminator = generated_request_schema.get("discriminator", {})
    mapping = discriminator.get("mapping", {})
    if not isinstance(mapping, dict) or not mapping:
        raise RuntimeError("browser request schema is missing its operation discriminator")

    components = openapi_schema["components"]["schemas"]
    payload_variants: list[dict[str, object]] = []
    contract_version_schema: dict[str, object] | None = None
    skill_binding_schema: dict[str, object] | None = None
    for request_ref in mapping.values():
        component_name = str(request_ref).rsplit("/", 1)[-1]
        request_properties = components[component_name]["properties"]
        if contract_version_schema is None:
            contract_version_schema = deepcopy(request_properties["contract_version"])
        payload_schema = deepcopy(request_properties["payload"])
        if payload_schema not in payload_variants:
            payload_variants.append(payload_schema)
        if "skill_binding" in request_properties:
            skill_binding_schema = deepcopy(request_properties["skill_binding"])

    properties: dict[str, object] = {
        "contract_version": contract_version_schema or {
            "type": "string",
            "enum": ["1.0"],
            "default": "1.0",
        },
        "operation": {
            "type": "string",
            "enum": list(mapping),
            "description": "Selects the operation-specific payload contract.",
        },
        "payload": {
            "type": "object",
            "oneOf": payload_variants,
            "description": "Payload fields must match the selected operation.",
        },
    }
    if skill_binding_schema is not None:
        properties["skill_binding"] = skill_binding_schema
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["operation", "payload"],
        "properties": properties,
    }


_BROWSER_ACTION_PATHS = ("/v1/browser/inspect", "/v1/browser/run")


def normalize_browser_action_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """Replace top-level request unions with GPT Actions-compatible objects."""

    for path in _BROWSER_ACTION_PATHS:
        request_body = schema["paths"][path]["post"]["requestBody"]
        generated_schema = request_body["content"]["application/json"]["schema"]
        request_body["required"] = True
        request_body["content"]["application/json"]["schema"] = (
            _request_object_schema(schema, generated_schema)
        )
    return schema


def register_browser_actions(app: FastAPI, service: BrowserActionService) -> None:
    app.state.browser_action_service = service

    @app.post(
        "/v1/browser/inspect",
        operation_id="inspectBrowserEvidence",
        response_model=BrowserActionResponse,
        response_model_exclude_none=True,
        summary="Inspect saved browser experiment evidence.",
        description=(
            "Read browser sessions, experiment manifests, and private stream status. "
            "Use the workspace Actions to inspect or modify files under the analysis directory."
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
