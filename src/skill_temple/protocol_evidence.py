"""Pure helpers for protocol evidence indexing and structured request replay."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit

from .browser_models import (
    ReplayBinding,
    ReplayMutation,
)
from .evidence_name_rules import is_sensitive_header
from .protocol.analyzers.response import analyze_replay_response  # noqa: F401
from .protocol.matching import (  # noqa: F401
    network_checkpoint,
    network_request_matches,
    requests_after_checkpoint,
    select_network_evidence,
)
from .protocol.mutations import (
    _decode_pointer,
    _encode_pointer_token,
    _last_pointer_token,
    _redact_json_value,
    _redact_scalar,
)
from .protocol.mutations import (
    redacted_mutation as _redacted_mutation,
)

_BROWSER_MANAGED_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "cookie",
    "host",
    "origin",
    "proxy-authorization",
    "referer",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def safe_token(value: str, *, fallback: str = "item") -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return token[:80] or fallback


def evidence_id(
    experiment_id: str,
    kind: str,
    *,
    selector_id: str | None = None,
    stable_id: str | int | None = None,
    ordinal: int = 1,
) -> str:
    parts = ["ev", safe_token(experiment_id), safe_token(kind)]
    if selector_id:
        parts.append(safe_token(selector_id))
    if stable_id is not None:
        parts.append(safe_token(str(stable_id)))
    else:
        parts.append(str(ordinal))
    return "_".join(parts)[:256]


def redact_header_entries(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    redacted: list[dict[str, str]] = []
    for item in entries:
        name = str(item.get("name", ""))
        value = str(item.get("value", ""))
        redacted.append(
            {
                "name": name,
                "value": "<redacted>" if is_sensitive_header(name) else value[:2048],
            }
        )
    return redacted


def public_network_summary(snapshot: dict[str, Any]) -> dict[str, Any]:
    request_headers = snapshot.get("requestHeadersArray")
    response_headers = snapshot.get("responseHeadersArray")
    return {
        "url": str(snapshot.get("url", ""))[:8192],
        "method": snapshot.get("method"),
        "resource_type": snapshot.get("resourceType"),
        "status": snapshot.get("status"),
        "status_text": snapshot.get("statusText"),
        "failure": snapshot.get("failure"),
        "request_headers": redact_header_entries(
            request_headers if isinstance(request_headers, list) else []
        ),
        "response_headers": redact_header_entries(
            response_headers if isinstance(response_headers, list) else []
        ),
        "request_body": _public_body_summary(snapshot.get("requestBody")),
        "request_shape": request_shape_from_snapshot(snapshot),
        "response_body": _public_body_summary(snapshot.get("responseBody")),
        "observed_at": snapshot.get("observedAt"),
        "timing": snapshot.get("timing"),
        "snapshot_integrity": network_snapshot_dimensions(snapshot),
    }


def network_snapshot_dimensions(snapshot: dict[str, Any]) -> dict[str, str]:
    method = str(snapshot.get("method", "GET")).upper()
    request_headers = snapshot.get("requestHeadersArray")
    explicit_headers_complete = snapshot.get("requestHeadersCompleteness") == "complete"
    extra_info = (
        snapshot.get("requestHeadersExtraInfo")
        or snapshot.get("requestExtraInfo")
        or snapshot.get("requestWillBeSentExtraInfo")
    )
    extra_headers = snapshot.get("requestHeadersExtraArray") or snapshot.get("requestHeadersExtra")
    associated_cookies = snapshot.get("associatedCookies")
    extra_info_complete = bool(
        isinstance(extra_info, dict)
        and isinstance(associated_cookies, list)
        and (isinstance(extra_headers, list) or isinstance(extra_info.get("headers"), (dict, list)))
    )
    headers_complete = bool(
        isinstance(request_headers, list) and (explicit_headers_complete or extra_info_complete)
    )
    request_body = snapshot.get("requestBody")
    if method in {"GET", "HEAD"} and not (
        isinstance(request_body, dict) and request_body.get("available")
    ):
        body_completeness = "not_required"
    elif isinstance(request_body, dict) and request_body.get("available"):
        body_completeness = "complete"
    elif isinstance(request_body, dict) and request_body.get("reason"):
        body_completeness = "partial"
    else:
        body_completeness = "unknown"
    request_headers_completeness = "complete" if headers_complete else "partial"
    response_headers_completeness = (
        "complete" if isinstance(snapshot.get("responseHeadersArray"), list) else "partial"
    )
    response_body = snapshot.get("responseBody")
    if isinstance(response_body, dict) and response_body.get("available"):
        response_body_completeness = "complete"
    elif isinstance(response_body, dict) and response_body.get("reason"):
        response_body_completeness = "partial"
    else:
        response_body_completeness = "unknown"
    network_snapshot_integrity = (
        "complete"
        if request_headers_completeness == "complete"
        and body_completeness in {"complete", "not_required"}
        else "partial"
    )
    return {
        "network_snapshot_integrity": network_snapshot_integrity,
        "request_body_completeness": body_completeness,
        "request_headers_completeness": request_headers_completeness,
        "response_body_completeness": response_body_completeness,
        "response_headers_completeness": response_headers_completeness,
    }


def stream_request_has_complete_request_headers(request: dict[str, Any]) -> bool:
    artifacts = request.get("coreArtifacts")
    if not isinstance(artifacts, list):
        return False
    by_kind = {
        str(item.get("kind")): item
        for item in artifacts
        if isinstance(item, dict) and item.get("kind")
    }
    request_headers = by_kind.get("request_headers")
    request_headers_extra = by_kind.get("request_headers_extra")
    if not isinstance(request_headers, dict) or not isinstance(
        request_headers_extra,
        dict,
    ):
        return False
    for descriptor in (request_headers, request_headers_extra):
        if descriptor.get("writeStatus") not in {None, "written"}:
            return False
        if isinstance(descriptor.get("bytes"), int) and descriptor["bytes"] <= 0:
            return False
    return True


def build_network_observation(
    *,
    observation_id: str,
    network_evidence: dict[str, Any] | None,
    stream_request: dict[str, Any] | None,
    association: dict[str, Any],
) -> dict[str, Any]:
    """Build one derived request view from network and stream source records."""

    network_evidence = network_evidence if isinstance(network_evidence, dict) else {}
    stream_request = stream_request if isinstance(stream_request, dict) else {}
    summary = network_evidence.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    snapshot = summary.get("snapshot_integrity")
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    request_headers = str(snapshot.get("request_headers_completeness") or "unknown")
    if stream_request_has_complete_request_headers(stream_request):
        request_headers = "complete"
    request_body = str(snapshot.get("request_body_completeness") or "unknown")
    response_headers = str(snapshot.get("response_headers_completeness") or "unknown")
    response_body = str(snapshot.get("response_body_completeness") or "unknown")
    network_artifacts = (
        "complete"
        if isinstance(network_evidence.get("artifact_paths"), dict)
        and network_evidence["artifact_paths"].get("all")
        else "partial"
        if network_evidence
        else "unknown"
    )
    has_stream = bool(stream_request)
    raw_stream = (
        str(stream_request.get("rawCaptureIntegrity") or "unknown")
        if has_stream
        else "not_required"
    )
    semantic_stream = (
        str(stream_request.get("semanticParseIntegrity") or "unknown")
        if has_stream
        else "not_required"
    )
    stream_artifacts = (
        str(stream_request.get("artifactIntegrity") or "unknown") if has_stream else "not_required"
    )
    completeness = {
        "request_headers": request_headers,
        "request_body": request_body,
        "response_headers": response_headers,
        "response_body": response_body,
        "network_artifacts": network_artifacts,
        "raw_stream": raw_stream,
        "semantic_stream": semantic_stream,
        "stream_artifacts": stream_artifacts,
    }
    missing_evidence = sorted(
        name for name, value in completeness.items() if value not in {"complete", "not_required"}
    )

    network_ids = network_evidence.get("request_ids")
    network_ids = network_ids if isinstance(network_ids, dict) else {}
    request_ids = {
        "reqid": network_ids.get("reqid"),
        "network_request_id": (
            network_ids.get("network_request_id") or stream_request.get("networkRequestId")
        ),
        "collector_generation": (
            network_ids.get("collector_generation")
            if network_ids.get("collector_generation") is not None
            else stream_request.get("collectorGeneration")
        ),
        "cdp_request_id": (network_ids.get("cdp_request_id") or stream_request.get("cdpRequestId")),
        "persistent_request_id": (
            network_ids.get("persistent_request_id") or stream_request.get("persistentRequestId")
        ),
    }
    network_artifact_ids = network_evidence.get("artifact_ids")
    network_artifact_ids = (
        [str(item) for item in network_artifact_ids]
        if isinstance(network_artifact_ids, list)
        else []
    )
    core_artifacts = stream_request.get("coreArtifacts")
    core_artifacts = (
        [item for item in core_artifacts if isinstance(item, dict)]
        if isinstance(core_artifacts, list)
        else []
    )
    stream_artifact_ids = [
        str(item.get("artifactId")) for item in core_artifacts if item.get("artifactId")
    ]
    association_status = str(association.get("status") or "not_found")
    association_method = association.get("method")
    confidence = (
        "exact"
        if association_status == "matched"
        and "url_method_fallback" not in str(association_method or "")
        else "heuristic"
        if association_status == "matched"
        else "ambiguous"
        if association_status == "ambiguous"
        else "missing"
    )
    return {
        "observation_id": observation_id,
        "request_ids": request_ids,
        "facts": {
            "url": str(summary.get("url") or stream_request.get("url") or "")[:8192],
            "method": summary.get("method") or stream_request.get("method"),
            "resource_type": summary.get("resource_type"),
            "http_status": (
                summary.get("status")
                if isinstance(summary.get("status"), int)
                else None
            ),
            "request_lifecycle_status": stream_request.get("status"),
            "status_text": summary.get("status_text"),
            "failure": summary.get("failure"),
            "observed_at": network_evidence.get("observed_at"),
            "terminal_reason": stream_request.get("terminalReason"),
            "primary_event_source": stream_request.get("primaryEventSource"),
            "raw_event_count": stream_request.get("rawEventCount"),
            "semantic_event_count": stream_request.get("semanticEventCount"),
            "experiment_cancellation_classification": stream_request.get(
                "experimentCancellationClassification"
            ),
            "request_body_canonical_sha256": network_evidence.get("request_body_canonical_sha256"),
        },
        "sources": {
            "network_evidence_id": network_evidence.get("evidence_id"),
            "stream_request_present": has_stream,
        },
        "artifact_ids": sorted(set(network_artifact_ids + stream_artifact_ids)),
        "association": {
            **association,
            "confidence": confidence,
        },
        "completeness": completeness,
        "missing_evidence": missing_evidence,
    }


def aggregate_observation_completeness(
    observations: list[dict[str, Any]],
    *,
    required_dimensions: set[str],
) -> tuple[dict[str, str], list[str]]:
    """Aggregate canonical observation dimensions without creating extra verdict fields."""

    severity = {
        "complete": 0,
        "not_required": 0,
        "partial": 2,
        "unknown": 2,
        "failed": 3,
    }
    dimensions: dict[str, str] = {}
    missing: set[str] = set()
    for name in sorted(required_dimensions):
        values = [
            str(item.get("completeness", {}).get(name) or "unknown")
            for item in observations
            if isinstance(item, dict) and isinstance(item.get("completeness"), dict)
        ]
        value = max(values, key=lambda item: severity.get(item, 2)) if values else "failed"
        dimensions[name] = value
        if value not in {"complete", "not_required"}:
            missing.add(name)
    return dimensions, sorted(missing)


def _public_body_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = {
        "available": bool(value.get("available")),
        "size": value.get("size"),
        "encoding": value.get("encoding"),
    }
    if not value.get("available"):
        result["reason"] = str(value.get("reason", ""))[:1000]
    return result


def load_snapshot(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Network evidence artifact must contain one JSON object")
    return value


def response_content_type(snapshot: dict[str, Any]) -> str | None:
    headers = snapshot.get("responseHeadersArray")
    if not isinstance(headers, list):
        return None
    for item in headers:
        if not isinstance(item, dict):
            continue
        if str(item.get("name", "")).lower() == "content-type":
            return str(item.get("value", "")).split(";", 1)[0].strip().lower()
    return None


def response_value_from_snapshot(
    snapshot: dict[str, Any] | None,
    *,
    max_bytes: int = 1_048_576,
) -> Any | None:
    if not isinstance(snapshot, dict):
        return None
    body = snapshot.get("responseBody")
    if not isinstance(body, dict) or not body.get("available"):
        return None
    encoding = body.get("encoding")
    if encoding == "utf8":
        text = str(body.get("text", ""))
        if len(text.encode("utf-8")) > max_bytes:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    if encoding == "base64":
        encoded = str(body.get("base64") or body.get("text") or "")
        if len(encoded) > max_bytes * 2:
            return None
        try:
            payload = base64.b64decode(encoded, validate=True)
        except Exception:
            return None
        if len(payload) > max_bytes:
            return None
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return None


def json_pointer_value(document: Any, path: str) -> Any:
    exists, value = _read_pointer(document, path)
    if not exists:
        raise ValueError(f"JSON Pointer path does not exist: {path}")
    return copy.deepcopy(value)


def binding_value_from_snapshot(
    snapshot: dict[str, Any],
    binding: ReplayBinding,
) -> Any:
    if binding.target == "json_pointer":
        body = _decode_json_request_body(snapshot.get("requestBody"))
        if body is None:
            raise ValueError("preserve_source JSON binding requires an available JSON body")
        exists, value = _read_pointer(body, str(binding.path))
        if not exists:
            raise ValueError(f"preserve_source binding path is missing: {binding.path}")
        return copy.deepcopy(value)
    if binding.target == "header":
        values = [
            item["value"]
            for item in _normalized_headers(snapshot.get("requestHeadersArray"))
            if item["name"].lower() == str(binding.name).lower()
        ]
        if not values:
            raise ValueError(f"preserve_source binding header is missing: {binding.name}")
        if binding.occurrence >= len(values):
            raise ValueError(
                f"preserve_source binding header occurrence is missing: "
                f"{binding.name}[{binding.occurrence}]"
            )
        return values[binding.occurrence]
    values = [
        value
        for name, value in parse_qsl(
            urlsplit(str(snapshot.get("url", ""))).query,
            keep_blank_values=True,
        )
        if name == binding.name
    ]
    if not values:
        raise ValueError(f"preserve_source query parameter is missing: {binding.name}")
    if binding.occurrence >= len(values):
        raise ValueError(
            f"preserve_source query occurrence is missing: {binding.name}[{binding.occurrence}]"
        )
    return values[binding.occurrence]


def request_shape_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    body = _decode_json_request_body(snapshot.get("requestBody"))
    if body is None:
        return None
    paths: dict[str, dict[str, Any]] = {}
    _collect_shape(body, "", paths, key_hint=None)
    return {"format": "json-pointer-v1", "paths": paths}


def redacted_request_body_from_snapshot(snapshot: dict[str, Any]) -> Any | None:
    body = _decode_json_request_body(snapshot.get("requestBody"))
    if body is None:
        return None
    return _redact_json_value(body, key_hint=None)


def _decode_json_request_body(value: Any) -> Any | None:
    if not isinstance(value, dict) or not value.get("available"):
        return None
    if value.get("encoding") != "utf8":
        return None
    try:
        return json.loads(str(value.get("text", "")))
    except json.JSONDecodeError:
        return None


def _collect_shape(
    value: Any,
    pointer: str,
    output: dict[str, dict[str, Any]],
    *,
    key_hint: str | None,
) -> None:
    if isinstance(value, dict):
        output[pointer or "/"] = {
            "type": "object",
            "keys": sorted(str(key) for key in value),
        }
        for key, child in value.items():
            token = _encode_pointer_token(str(key))
            _collect_shape(child, f"{pointer}/{token}", output, key_hint=str(key))
        return
    if isinstance(value, list):
        output[pointer or "/"] = {"type": "array", "length": len(value)}
        for index, child in enumerate(value):
            _collect_shape(child, f"{pointer}/{index}", output, key_hint=key_hint)
        return
    entry = {"type": _json_type(value)}
    entry["value"] = _redact_scalar(value, key_hint=key_hint)
    output[pointer or "/"] = entry


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def build_replay_spec(
    snapshot: dict[str, Any],
    mutations: list[ReplayMutation],
    *,
    bindings: list[ReplayBinding] | None = None,
    binding_values: dict[str, Any] | None = None,
    query_serialization: str = "preserve_raw",
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = str(snapshot.get("url", ""))
    method = str(snapshot.get("method", "GET")).upper()
    headers = _normalized_headers(snapshot.get("requestHeadersArray"))
    body = copy.deepcopy(snapshot.get("requestBody"))
    source_url = url
    source_headers = copy.deepcopy(headers)
    source_body = copy.deepcopy(body)
    ignored_headers: list[str] = []

    for binding in bindings or []:
        if binding_values is None or binding.binding_id not in binding_values:
            raise ValueError(f"Missing binding value: {binding.binding_id}")
        binding_value = binding_values[binding.binding_id]
        if binding.target == "json_pointer":
            body = _replace_json_pointer(body, str(binding.path), binding_value)
        elif binding.target == "header":
            headers = _replace_header(
                headers,
                str(binding.name),
                str(binding_value),
                occurrence=binding.occurrence,
            )
        else:
            url = _mutate_query(
                url,
                str(binding.name),
                str(binding_value),
                operation="replace",
                occurrence=binding.occurrence,
                serialization=query_serialization,
            )

    for mutation in mutations:
        if mutation.type in {"remove_header", "replace_header", "add_header"}:
            headers = _mutate_headers(headers, mutation)
        elif mutation.type in {
            "remove_query_parameter",
            "replace_query_parameter",
            "add_query_parameter",
        }:
            operation = mutation.type.split("_", 1)[0]
            url = _mutate_query(
                url,
                mutation.name,
                getattr(mutation, "value", None),
                operation=operation,
                occurrence=mutation.occurrence,
                serialization=query_serialization,
            )
        elif mutation.type in {
            "remove_json_path",
            "replace_json_path",
            "add_json_path",
        }:
            body = _mutate_json_body(body, mutation)

    replay_headers: list[dict[str, str]] = []
    for item in headers:
        normalized = item["name"].lower()
        if normalized in _BROWSER_MANAGED_HEADERS or normalized.startswith("sec-"):
            ignored_headers.append(item["name"])
            continue
        replay_headers.append(item)

    spec = {
        "url": url,
        "method": method,
        "headers": replay_headers,
        "body": body if isinstance(body, dict) and body.get("available") else None,
        "querySerialization": query_serialization,
    }
    diff = {
        "source": {
            "url": source_url[:8192],
            "method": method,
            "header_names": [item["name"] for item in source_headers],
            "request_body": _public_body_summary(source_body),
        },
        "replay": {
            "url": url[:8192],
            "method": method,
            "header_names": [item["name"] for item in replay_headers],
            "ignored_browser_managed_headers": sorted(set(ignored_headers)),
            "request_body": _public_body_summary(body),
            "query_serialization": query_serialization,
        },
        "mutations": [_redacted_mutation(mutation) for mutation in mutations],
        "bindings": [
            {
                "binding_id": binding.binding_id,
                "target": binding.target,
                "path": binding.path,
                "name": binding.name,
                "generator": binding.generator,
                "value_source": binding.value_source,
                "value": "<bound>",
            }
            for binding in bindings or []
        ],
    }
    return spec, diff


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def request_body_canonical_sha256_from_spec(spec: dict[str, Any]) -> str | None:
    return _body_canonical_sha256(spec.get("body"))


def request_body_canonical_sha256_from_snapshot(
    snapshot: dict[str, Any],
) -> str | None:
    return _body_canonical_sha256(snapshot.get("requestBody"))


def _body_canonical_sha256(body: Any) -> str | None:
    if not isinstance(body, dict) or not body.get("available"):
        return None
    if body.get("encoding") == "utf8":
        text = str(body.get("text", ""))
        try:
            return canonical_json_sha256(json.loads(text))
        except json.JSONDecodeError:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()
    if body.get("encoding") == "base64":
        return str(body.get("sha256") or body.get("encodedSha256") or "") or None
    return None


def _replay_operation_target(operation: ReplayBinding | ReplayMutation) -> dict[str, Any]:
    if isinstance(operation, ReplayBinding):
        return {
            "kind": operation.target,
            "name": (
                str(operation.path)
                if operation.target == "json_pointer"
                else str(operation.name).lower()
                if operation.target == "header"
                else str(operation.name)
            ),
            "occurrence": operation.occurrence,
            "action": "binding",
        }
    if operation.type.endswith("json_path"):
        return {
            "kind": "json_pointer",
            "name": str(operation.path),
            "occurrence": None,
            "action": operation.type.split("_", 1)[0],
        }
    kind = "header" if operation.type.endswith("header") else "query_parameter"
    return {
        "kind": kind,
        "name": (
            str(operation.name).lower() if kind == "header" else str(operation.name)
        ),
        "occurrence": operation.occurrence,
        "action": operation.type.split("_", 1)[0],
    }


def replay_operation_overwritten_by_later(
    operation: ReplayBinding | ReplayMutation,
    later_operation: ReplayMutation,
) -> bool:
    current = _replay_operation_target(operation)
    later = _replay_operation_target(later_operation)
    if current["kind"] != later["kind"] or later["action"] == "add":
        return False
    if current["kind"] == "json_pointer":
        current_tokens = _decode_pointer(str(current["name"]))
        later_tokens = _decode_pointer(str(later["name"]))
        return (
            current_tokens[: len(later_tokens)] == later_tokens
            or later_tokens[: len(current_tokens)] == current_tokens
        )
    if current["name"] != later["name"]:
        return False
    later_occurrence = later["occurrence"]
    current_occurrence = current["occurrence"]
    return later_occurrence == "all" or (
        isinstance(current_occurrence, int)
        and isinstance(later_occurrence, int)
        and current_occurrence == later_occurrence
    )


def assess_mutation_effectiveness(
    mutation: ReplayMutation | None,
    wire_snapshot: dict[str, Any] | None,
    *,
    overwritten_by_later: bool = False,
) -> dict[str, Any]:
    if mutation is None:
        return {
            "mutation_requested": None,
            "mutation_observed_on_wire": None,
            "mutation_effective": None,
            "reason": "replay has no mutation to observe",
        }
    requested = _redacted_mutation(mutation)
    if overwritten_by_later:
        return {
            "mutation_requested": requested,
            "operation_applied_to_spec": True,
            "mutation_observed_on_wire": None,
            "mutation_effective": None,
            "final_wire_observability": "overwritten_by_later_operation",
            "reason": (
                "mutation was applied in order but its intermediate value was "
                "overwritten by a later operation"
            ),
        }
    if wire_snapshot is None:
        return {
            "mutation_requested": requested,
            "operation_applied_to_spec": True,
            "mutation_observed_on_wire": None,
            "mutation_effective": False,
            "final_wire_observability": "unavailable",
            "reason": "exact replay request snapshot was not exported",
        }
    try:
        if mutation.type in {
            "remove_json_path",
            "replace_json_path",
            "add_json_path",
        }:
            body_value = _decode_json_request_body(wire_snapshot.get("requestBody"))
            if body_value is None:
                raise ValueError("wire request has no JSON body")
            exists, observed = _read_pointer(body_value, mutation.path)
            effective = (
                not exists
                if mutation.type == "remove_json_path"
                else exists and observed == mutation.value
            )
            observed_public = (
                "<absent>"
                if not exists
                else _redact_json_value(observed, key_hint=_last_pointer_token(mutation.path))
            )
        elif mutation.type in {"remove_header", "replace_header", "add_header"}:
            _, values = _observe_mutation_target(wire_snapshot, mutation)
            effective = (
                not values
                if mutation.type == "remove_header"
                else _occurrence_value_matches(
                    values,
                    mutation.value,
                    mutation.occurrence,
                )
            )
            observed_public = "<absent>" if not values else "<present>"
        else:
            _, values = _observe_mutation_target(wire_snapshot, mutation)
            effective = (
                not values
                if mutation.type == "remove_query_parameter"
                else _occurrence_value_matches(
                    values,
                    mutation.value,
                    mutation.occurrence,
                )
            )
            observed_public = "<absent>" if not values else "<present>"
        return {
            "mutation_requested": requested,
            "operation_applied_to_spec": True,
            "mutation_observed_on_wire": observed_public,
            "mutation_effective": effective,
            "final_wire_observability": "observed" if effective else "contradicted",
            "reason": (
                "wire request matches requested mutation"
                if effective
                else "wire request does not match requested mutation"
            ),
        }
    except ValueError as exc:
        return {
            "mutation_requested": requested,
            "operation_applied_to_spec": True,
            "mutation_observed_on_wire": None,
            "mutation_effective": False,
            "final_wire_observability": "unavailable",
            "reason": str(exc),
        }


def observe_binding_application(
    wire_snapshot: dict[str, Any] | None,
    *,
    bindings: list[ReplayBinding],
    binding_values: dict[str, Any],
    mutations: list[ReplayMutation] | None = None,
) -> dict[str, Any]:
    mutations = mutations or []
    if wire_snapshot is None:
        return {
            "binding_count": len(bindings),
            "binding_application_complete": False,
            "binding_observations": [
                {
                    "binding_id": binding.binding_id,
                    "operation_applied_to_spec": True,
                    "final_wire_observability": "unavailable",
                }
                for binding in bindings
            ],
            "reason": "exact replay request snapshot is required",
        }
    observations: list[dict[str, Any]] = []
    for binding in bindings:
        overwritten = any(
            replay_operation_overwritten_by_later(binding, mutation)
            for mutation in mutations
        )
        if overwritten:
            observations.append(
                {
                    "binding_id": binding.binding_id,
                    "operation_applied_to_spec": True,
                    "binding_observed_on_final_wire": None,
                    "final_wire_observability": "overwritten_by_later_operation",
                }
            )
            continue
        visible = _bindings_match_snapshot(
            wire_snapshot,
            [binding],
            binding_values,
        )
        observations.append(
            {
                "binding_id": binding.binding_id,
                "operation_applied_to_spec": True,
                "binding_observed_on_final_wire": visible,
                "final_wire_observability": "observed" if visible else "contradicted",
            }
        )
    bindings_effective = all(
        item["final_wire_observability"]
        in {"observed", "overwritten_by_later_operation"}
        for item in observations
    )
    return {
        "binding_count": len(bindings),
        "binding_application_complete": bindings_effective,
        "binding_observations": observations,
        "reason": (
            "all resolved bindings were applied; visible values match the final wire"
            if bindings_effective
            else "one or more resolved bindings contradict the final wire request"
        ),
    }


def _observe_mutation_target(
    snapshot: dict[str, Any],
    mutation: ReplayMutation,
) -> tuple[bool, Any]:
    if mutation.type in {
        "remove_json_path",
        "replace_json_path",
        "add_json_path",
    }:
        body = _decode_json_request_body(snapshot.get("requestBody"))
        if body is None:
            raise ValueError("wire request has no JSON body")
        return _read_pointer(body, mutation.path)
    if mutation.type in {"remove_header", "replace_header", "add_header"}:
        values = [
            item["value"]
            for item in _normalized_headers(snapshot.get("requestHeadersArray"))
            if item["name"].lower() == mutation.name.lower()
        ]
        return bool(values), values
    values = [
        value
        for name, value in parse_qsl(
            urlsplit(str(snapshot.get("url", ""))).query,
            keep_blank_values=True,
        )
        if name == mutation.name
    ]
    return bool(values), values


def _bindings_match_snapshot(
    snapshot: dict[str, Any],
    bindings: list[ReplayBinding],
    expected_values: dict[str, Any],
) -> bool:
    for binding in bindings:
        if binding.binding_id not in expected_values:
            return False
        expected = expected_values[binding.binding_id]
        if binding.target == "json_pointer":
            body = _decode_json_request_body(snapshot.get("requestBody"))
            if body is None:
                return False
            exists, observed = _read_pointer(body, str(binding.path))
        elif binding.target == "header":
            values = [
                item["value"]
                for item in _normalized_headers(snapshot.get("requestHeadersArray"))
                if item["name"].lower() == str(binding.name).lower()
            ]
            exists = binding.occurrence < len(values)
            observed = values[binding.occurrence] if exists else None
            expected = str(expected)
        else:
            values = [
                value
                for name, value in parse_qsl(
                    urlsplit(str(snapshot.get("url", ""))).query,
                    keep_blank_values=True,
                )
                if name == binding.name
            ]
            exists = binding.occurrence < len(values)
            observed = values[binding.occurrence] if exists else None
            expected = str(expected)
        if not exists or observed != expected:
            return False
    return True


def _normalized_headers(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"name": str(item.get("name", "")), "value": str(item.get("value", ""))}
        for item in value
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]


def _replace_header(
    headers: list[dict[str, str]],
    name: str,
    value: str,
    *,
    occurrence: int = 0,
) -> list[dict[str, str]]:
    result = copy.deepcopy(headers)
    matches = [index for index, item in enumerate(result) if item["name"].lower() == name.lower()]
    if occurrence >= len(matches):
        raise ValueError(f"Header occurrence does not exist: {name}[{occurrence}]")
    result[matches[occurrence]] = {"name": name, "value": value}
    return result


def _mutate_headers(
    headers: list[dict[str, str]],
    mutation: ReplayMutation,
) -> list[dict[str, str]]:
    result = copy.deepcopy(headers)
    matches = [
        index for index, item in enumerate(result) if item["name"].lower() == mutation.name.lower()
    ]
    occurrence = mutation.occurrence
    if mutation.type == "add_header":
        result.append({"name": mutation.name, "value": mutation.value})
        return result
    if occurrence == "all":
        if mutation.type == "remove_header":
            return [item for item in result if item["name"].lower() != mutation.name.lower()]
        if not matches:
            raise ValueError(f"Header does not exist: {mutation.name}")
        for index in matches:
            result[index] = {"name": mutation.name, "value": mutation.value}
        return result
    if occurrence >= len(matches):
        raise ValueError(f"Header occurrence does not exist: {mutation.name}[{occurrence}]")
    target_index = matches[occurrence]
    if mutation.type == "remove_header":
        del result[target_index]
    else:
        result[target_index] = {"name": mutation.name, "value": mutation.value}
    return result


def _mutate_query(
    url: str,
    name: str,
    value: str | None,
    *,
    operation: str,
    occurrence: int | str,
    serialization: str = "preserve_raw",
) -> str:
    split = urlsplit(url)
    if serialization == "preserve_raw":
        raw_entries = split.query.split("&") if split.query else []
        decoded_names = [
            unquote_plus(item.partition("=")[0])
            for item in raw_entries
        ]
        matches = [index for index, key in enumerate(decoded_names) if key == name]
        encoded_pair = urlencode([(name, value or "")])
        if operation == "add":
            raw_entries.append(encoded_pair)
        elif occurrence == "all":
            if operation == "remove":
                raw_entries = [
                    item
                    for index, item in enumerate(raw_entries)
                    if index not in set(matches)
                ]
            else:
                if not matches:
                    raise ValueError(f"Query parameter does not exist: {name}")
                for index in matches:
                    raw_key = raw_entries[index].partition("=")[0]
                    raw_entries[index] = f"{raw_key}={encoded_pair.partition('=')[2]}"
        else:
            selected = int(occurrence)
            if selected >= len(matches):
                raise ValueError(f"Query occurrence does not exist: {name}[{selected}]")
            target_index = matches[selected]
            if operation == "remove":
                del raw_entries[target_index]
            else:
                raw_key = raw_entries[target_index].partition("=")[0]
                raw_entries[target_index] = f"{raw_key}={encoded_pair.partition('=')[2]}"
        return urlunsplit(
            (
                split.scheme,
                split.netloc,
                split.path,
                "&".join(raw_entries),
                split.fragment,
            )
        )
    if serialization != "normalize":
        raise ValueError(f"Unsupported query serialization mode: {serialization}")
    entries = list(parse_qsl(split.query, keep_blank_values=True))
    matches = [index for index, (key, _) in enumerate(entries) if key == name]
    if operation == "add":
        entries.append((name, value or ""))
    elif occurrence == "all":
        if operation == "remove":
            entries = [(key, item) for key, item in entries if key != name]
        else:
            if not matches:
                raise ValueError(f"Query parameter does not exist: {name}")
            for index in matches:
                entries[index] = (name, value or "")
    else:
        selected = int(occurrence)
        if selected >= len(matches):
            raise ValueError(f"Query occurrence does not exist: {name}[{selected}]")
        target_index = matches[selected]
        if operation == "remove":
            del entries[target_index]
        else:
            entries[target_index] = (name, value or "")
    return urlunsplit(
        (
            split.scheme,
            split.netloc,
            split.path,
            urlencode(entries, doseq=True),
            split.fragment,
        )
    )


def _mutate_json_body(body: Any, mutation: ReplayMutation) -> dict[str, Any]:
    if not isinstance(body, dict) or not body.get("available"):
        raise ValueError("JSON mutation requires an available request body")
    if body.get("encoding") != "utf8":
        raise ValueError("JSON mutation requires a UTF-8 request body")
    try:
        value = json.loads(str(body.get("text", "")))
    except json.JSONDecodeError as exc:
        raise ValueError("JSON mutation requires a valid JSON request body") from exc
    path = str(getattr(mutation, "path", ""))
    if mutation.type == "remove_json_path":
        _remove_pointer(value, path)
    elif mutation.type == "add_json_path":
        _add_pointer(value, path, mutation.value)
    else:
        _replace_pointer(value, path, mutation.value)
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {
        "available": True,
        "size": len(encoded.encode("utf-8")),
        "encoding": "utf8",
        "text": encoded,
    }


def _replace_json_pointer(body: Any, path: str, value: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or not body.get("available"):
        raise ValueError("JSON volatile binding requires an available request body")
    if body.get("encoding") != "utf8":
        raise ValueError("JSON volatile binding requires a UTF-8 request body")
    try:
        decoded = json.loads(str(body.get("text", "")))
    except json.JSONDecodeError as exc:
        raise ValueError("JSON volatile binding requires a valid JSON request body") from exc
    _replace_pointer(decoded, path, value)
    encoded = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"))
    return {
        "available": True,
        "size": len(encoded.encode("utf-8")),
        "encoding": "utf8",
        "text": encoded,
    }


def _resolve_parent(document: Any, path: str) -> tuple[Any, str]:
    tokens = _decode_pointer(path)
    parent = document
    for token in tokens[:-1]:
        if isinstance(parent, dict):
            if token not in parent:
                raise ValueError(f"JSON Pointer path does not exist: {path}")
            parent = parent[token]
        elif isinstance(parent, list):
            index = _parse_list_index(token, len(parent), path)
            parent = parent[index]
        else:
            raise ValueError(f"JSON Pointer traverses a scalar value: {path}")
    return parent, tokens[-1]


def _parse_list_index(token: str, length: int, path: str) -> int:
    if not token.isdigit():
        raise ValueError(f"JSON Pointer array token must be a non-negative index: {path}")
    index = int(token)
    if index >= length:
        raise ValueError(f"JSON Pointer array index is out of range: {path}")
    return index


def _read_pointer(document: Any, path: str) -> tuple[bool, Any]:
    try:
        parent, leaf = _resolve_parent(document, path)
        if isinstance(parent, dict):
            return (leaf in parent, parent.get(leaf))
        if isinstance(parent, list):
            index = _parse_list_index(leaf, len(parent), path)
            return True, parent[index]
        return False, None
    except ValueError:
        return False, None


def _remove_pointer(document: Any, path: str) -> None:
    parent, leaf = _resolve_parent(document, path)
    if isinstance(parent, dict):
        if leaf not in parent:
            raise ValueError(f"JSON Pointer path does not exist: {path}")
        del parent[leaf]
        return
    if isinstance(parent, list):
        del parent[_parse_list_index(leaf, len(parent), path)]
        return
    raise ValueError(f"JSON Pointer parent is not a container: {path}")


def _replace_pointer(document: Any, path: str, value: Any) -> None:
    parent, leaf = _resolve_parent(document, path)
    if isinstance(parent, dict):
        if leaf not in parent:
            raise ValueError(f"JSON Pointer path does not exist: {path}")
        parent[leaf] = value
        return
    if isinstance(parent, list):
        parent[_parse_list_index(leaf, len(parent), path)] = value
        return
    raise ValueError(f"JSON Pointer parent is not a container: {path}")


def _add_pointer(document: Any, path: str, value: Any) -> None:
    parent, leaf = _resolve_parent(document, path)
    if isinstance(parent, dict):
        parent[leaf] = value
        return
    if isinstance(parent, list):
        if leaf == "-":
            parent.append(value)
            return
        if not leaf.isdigit():
            raise ValueError(f"JSON Pointer array token must be an index or '-': {path}")
        index = int(leaf)
        if index > len(parent):
            raise ValueError(f"JSON Pointer array index is out of range: {path}")
        parent.insert(index, value)
        return
    raise ValueError(f"JSON Pointer parent is not a container: {path}")


def _occurrence_value_matches(
    values: list[Any],
    expected: Any,
    occurrence: int | str,
) -> bool:
    if occurrence in {"all", "append"}:
        return bool(values) and (
            all(value == expected for value in values)
            if occurrence == "all"
            else values[-1] == expected
        )
    return occurrence < len(values) and values[occurrence] == expected


def _remove_named_occurrence(
    entries: list[Any],
    name: str,
    occurrence: int | str,
    *,
    case_sensitive: bool,
) -> list[Any]:
    def entry_name(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get("name", ""))
        return str(item[0])

    def matches(item: Any) -> bool:
        current = entry_name(item)
        return current == name if case_sensitive else current.lower() == name.lower()

    result = copy.deepcopy(entries)
    indexes = [index for index, item in enumerate(result) if matches(item)]
    if occurrence == "all":
        return [item for item in result if not matches(item)]
    if occurrence == "append":
        if indexes:
            del result[indexes[-1]]
        return result
    if occurrence < len(indexes):
        del result[indexes[occurrence]]
    return result
