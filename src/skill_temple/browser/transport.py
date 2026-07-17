"""Strict public Browser Action envelope decoding through the operation registry."""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from ..browser_models import (
    InspectBrowserEvidenceEnvelope,
    InspectBrowserEvidenceRequest,
    RunBrowserExperimentEnvelope,
    RunBrowserExperimentRequest,
)
from .contracts import expected_binding
from .registry import OPERATION_REGISTRY, ActionKind, OperationSpec

RUN_OPERATIONS = frozenset(OPERATION_REGISTRY.operations("run"))
INSPECT_OPERATIONS = frozenset(OPERATION_REGISTRY.operations("inspect"))

RequestT = TypeVar("RequestT", bound=BaseModel)
BrowserEnvelope = RunBrowserExperimentEnvelope | InspectBrowserEvidenceEnvelope


class BrowserTransportError(ValueError):
    """Structured failure guaranteed to occur before browser dispatch."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation: str,
        status_code: int = 400,
        issues: list[dict[str, str]] | None = None,
        suggested_next_action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation = operation
        self.status_code = status_code
        self.issues = issues or []
        self.suggested_next_action = suggested_next_action or _contract_suggestion(operation)
        self.details = details or {}

    def response_content(self) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "operation": self.operation,
            "message": str(self),
            "dispatch_started": False,
            "suggested_next_action": self.suggested_next_action,
            **self.details,
        }
        if self.issues:
            error["issues"] = self.issues
        return {"error": error}


def _contract_suggestion(operation: str) -> str:
    spec = OPERATION_REGISTRY.get(operation)
    if spec is None:
        return (
            "Read browser-action-protocol/docs/operation-index.md and choose an "
            "operation for the correct Browser Action."
        )
    return f"Read browser-action-protocol/{spec.contract_doc_path} and correct payload_json."


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _decode_payload(operation: str, payload_json: str) -> dict[str, Any]:
    try:
        decoded = json.loads(
            payload_json,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise BrowserTransportError(
            "invalid_json",
            f"payload_json is not strict JSON: {exc}",
            operation=operation,
        ) from exc
    if not isinstance(decoded, dict):
        raise BrowserTransportError(
            "payload_must_be_object",
            "payload_json must decode to a JSON object.",
            operation=operation,
        )
    return decoded


def _pointer(loc: tuple[Any, ...], operation: str) -> str:
    parts = [str(item) for item in loc]
    if parts and parts[0] == operation:
        parts = parts[1:]
    if parts and parts[0] == "payload":
        parts = parts[1:]
    escaped = [part.replace("~", "~0").replace("/", "~1") for part in parts]
    return "/" + "/".join(escaped) if escaped else "/"


def _validation_issues(exc: ValidationError, operation: str) -> list[dict[str, str]]:
    return [
        {
            "path": _pointer(tuple(error.get("loc", ())), operation),
            "type": str(error.get("type", "validation_error")),
            "message": str(error.get("msg", "Invalid operation payload")),
        }
        for error in exc.errors(include_url=False, include_context=False)
    ]


def _require_spec(operation: str, action: ActionKind) -> OperationSpec:
    spec = OPERATION_REGISTRY.get(operation)
    if spec is None:
        raise BrowserTransportError(
            "unknown_operation",
            "Unknown Browser operation.",
            operation=operation,
            suggested_next_action=(
                "Read browser-action-protocol/docs/operation-index.md and choose an "
                "operation for the correct Browser Action."
            ),
        )
    if spec.action != action:
        raise BrowserTransportError(
            "unknown_operation",
            "Operation belongs to the other Browser Action.",
            operation=operation,
            suggested_next_action=(
                "Read browser-action-protocol/docs/operation-index.md and use the "
                "Action assigned to this operation."
            ),
        )
    return spec


def _validate_binding(
    envelope: BrowserEnvelope,
    spec: OperationSpec,
    *,
    skill_content_hash: str | None,
) -> dict[str, str]:
    expected = expected_binding(spec, skill_content_hash=skill_content_hash)
    actual = {
        "skill_id": str(envelope.skill_id),
        "skill_content_hash": str(envelope.skill_content_hash),
        "operation_contract_hash": str(envelope.operation_contract_hash),
    }
    mismatched = [key for key, value in actual.items() if value != expected[key]]
    if mismatched:
        raise BrowserTransportError(
            "stale_operation_contract",
            "The loaded protocol Skill or operation contract does not match this server build.",
            operation=spec.name,
            status_code=409,
            suggested_next_action=(
                "Reload the exact browser-action-protocol Skill and operation contract "
                "before retrying."
            ),
            details={
                "mismatched_fields": mismatched,
                "expected_skill_id": expected["skill_id"],
                "expected_skill_content_hash": expected["skill_content_hash"],
                "expected_contract_hash": expected["operation_contract_hash"],
            },
        )
    return expected


def _decode_envelope(
    *,
    envelope: BrowserEnvelope,
    action: ActionKind,
    skill_content_hash: str | None,
) -> BaseModel:
    operation = str(envelope.operation)
    spec = _require_spec(operation, action)
    binding = _validate_binding(
        envelope,
        spec,
        skill_content_hash=skill_content_hash,
    )
    payload = _decode_payload(operation, str(envelope.payload_json))
    adapter: TypeAdapter[BaseModel] = TypeAdapter(spec.request_model)
    try:
        return adapter.validate_python(
            {
                "contract_version": "1.0",
                "operation": operation,
                "payload": payload,
                "action_binding": binding,
            }
        )
    except ValidationError as exc:
        raise BrowserTransportError(
            "invalid_operation_payload",
            "Decoded payload does not satisfy the operation contract.",
            operation=operation,
            status_code=422,
            issues=_validation_issues(exc, operation),
        ) from exc


def decode_run_envelope(
    envelope: RunBrowserExperimentEnvelope,
    *,
    skill_content_hash: str | None = None,
) -> RunBrowserExperimentRequest:
    return _decode_envelope(  # type: ignore[return-value]
        envelope=envelope,
        action="run",
        skill_content_hash=skill_content_hash,
    )


def decode_inspect_envelope(
    envelope: InspectBrowserEvidenceEnvelope,
    *,
    skill_content_hash: str | None = None,
) -> InspectBrowserEvidenceRequest:
    return _decode_envelope(  # type: ignore[return-value]
        envelope=envelope,
        action="inspect",
        skill_content_hash=skill_content_hash,
    )
