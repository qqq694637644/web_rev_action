"""Pure helpers for protocol evidence indexing and structured request replay."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

from .evidence_name_rules import is_sensitive_header
from .protocol import shapes


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
        "request_body": shapes.public_body_summary(snapshot.get("requestBody")),
        "request_shape": shapes.request_shape_from_snapshot(snapshot),
        "response_body": shapes.public_body_summary(snapshot.get("responseBody")),
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
