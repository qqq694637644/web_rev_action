"""Pure helpers for protocol evidence indexing and structured request replay."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .browser_models import (
    NetworkEvidenceSelector,
    ReplayMutation,
    RequestMatcher,
    VolatileBinding,
)
from .evidence_name_rules import is_sensitive_header

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


def network_request_matches(request: dict[str, Any], matcher: RequestMatcher) -> bool:
    if matcher.request_id and str(request.get("reqid")) != matcher.request_id:
        return False
    if matcher.url_contains and matcher.url_contains not in str(request.get("url", "")):
        return False
    if matcher.method and matcher.method != str(request.get("method", "")).upper():
        return False
    if matcher.resource_types and str(request.get("resourceType", "")).lower() not in {
        item.lower() for item in matcher.resource_types
    }:
        return False
    return True


def network_checkpoint(requests: list[dict[str, Any]], *, generation: int) -> dict[str, Any]:
    reqids = sorted(int(item["reqid"]) for item in requests if isinstance(item.get("reqid"), int))
    return {
        "collector_generation": generation,
        "max_reqid": max(reqids, default=0),
        "existing_reqids": reqids,
        "in_flight_reqids": sorted(
            int(item["reqid"])
            for item in requests
            if isinstance(item.get("reqid"), int) and bool(item.get("pending"))
        ),
    }


def requests_after_checkpoint(
    requests: list[dict[str, Any]],
    checkpoint: dict[str, Any],
    *,
    include_in_flight: bool,
) -> list[dict[str, Any]]:
    max_reqid = int(checkpoint.get("max_reqid", 0) or 0)
    allowed_in_flight = {
        int(item) for item in checkpoint.get("in_flight_reqids", []) if isinstance(item, int)
    }
    selected = []
    for item in requests:
        reqid = item.get("reqid")
        if not isinstance(reqid, int):
            continue
        if reqid > max_reqid or (include_in_flight and reqid in allowed_in_flight):
            selected.append(item)
    return sorted(selected, key=lambda item: int(item.get("reqid", 0)))


def select_network_evidence(
    requests: list[dict[str, Any]], selector: NetworkEvidenceSelector
) -> list[dict[str, Any]]:
    return [item for item in requests if network_request_matches(item, selector.matcher)][
        : selector.max_matches
    ]


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
    binding: VolatileBinding,
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


def validate_binding_mutation_compatibility(
    bindings: list[VolatileBinding],
    mutation: ReplayMutation | None,
) -> None:
    if mutation is None or mutation.type not in {
        "remove_json_path",
        "replace_json_path",
        "add_json_path",
    }:
        return
    mutation_tokens = _decode_pointer(mutation.path)
    for binding in bindings:
        if binding.target != "json_pointer":
            continue
        binding_tokens = _decode_pointer(str(binding.path))
        if (
            len(binding_tokens) < len(mutation_tokens)
            and mutation_tokens[: len(binding_tokens)] == binding_tokens
        ):
            raise ValueError(
                "volatile binding path contains the mutation target; declare a narrower "
                "binding or remove the overlap"
            )


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


def _redact_json_value(value: Any, *, key_hint: str | None) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _redact_json_value(child, key_hint=str(key)) for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(child, key_hint=key_hint) for child in value]
    return _redact_scalar(value, key_hint=key_hint)


def _redact_scalar(value: Any, *, key_hint: str | None) -> Any:
    raw_hint = key_hint or ""
    normalized = raw_hint.lower()
    if any(
        fragment in normalized
        for fragment in (
            "token",
            "secret",
            "password",
            "api-key",
            "apikey",
            "cookie",
            "authorization",
            "csrf",
            "xsrf",
            "session",
            "signature",
        )
    ):
        return "<redacted>"
    identifier_key = bool(
        raw_hint == "id" or normalized.endswith("_id") or re.search(r"(?:Id|ID)$", raw_hint)
    )
    if identifier_key:
        return "<identifier>"
    if not isinstance(value, str):
        return value
    if normalized in {"content", "parts", "text", "prompt", "query", "title"}:
        return "<text>"
    return "<string>"


def build_replay_spec(
    snapshot: dict[str, Any],
    mutations: list[ReplayMutation],
    *,
    volatile_bindings: list[VolatileBinding] | None = None,
    binding_values: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = str(snapshot.get("url", ""))
    method = str(snapshot.get("method", "GET")).upper()
    headers = _normalized_headers(snapshot.get("requestHeadersArray"))
    body = copy.deepcopy(snapshot.get("requestBody"))
    source_url = url
    source_headers = copy.deepcopy(headers)
    source_body = copy.deepcopy(body)
    ignored_headers: list[str] = []

    for binding in volatile_bindings or []:
        if binding_values is None or binding.binding_id not in binding_values:
            raise ValueError(f"Missing volatile binding value: {binding.binding_id}")
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
        },
        "mutations": [_redacted_mutation(mutation) for mutation in mutations],
        "volatile_bindings": [
            {
                "binding_id": binding.binding_id,
                "target": binding.target,
                "path": binding.path,
                "name": binding.name,
                "generator": binding.generator,
                "value": "<generated>",
            }
            for binding in volatile_bindings or []
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


def assess_mutation_effectiveness(
    mutation: ReplayMutation | None,
    wire_snapshot: dict[str, Any] | None,
) -> dict[str, Any]:
    if mutation is None:
        return {
            "mutation_requested": None,
            "mutation_observed_on_wire": None,
            "mutation_effective": None,
            "reason": "control replay has no classification mutation",
        }
    requested = _redacted_mutation(mutation)
    if wire_snapshot is None:
        return {
            "mutation_requested": requested,
            "mutation_observed_on_wire": None,
            "mutation_effective": False,
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
            "mutation_observed_on_wire": observed_public,
            "mutation_effective": effective,
            "reason": (
                "wire request matches requested mutation"
                if effective
                else "wire request does not match requested mutation"
            ),
        }
    except ValueError as exc:
        return {
            "mutation_requested": requested,
            "mutation_observed_on_wire": None,
            "mutation_effective": False,
            "reason": str(exc),
        }


def assess_paired_mutation_effectiveness(
    mutation: ReplayMutation,
    control_snapshot: dict[str, Any] | None,
    treatment_snapshot: dict[str, Any] | None,
    *,
    volatile_bindings: list[VolatileBinding],
    control_binding_values: dict[str, Any],
    treatment_binding_values: dict[str, Any],
    normalize_wire_order: bool = False,
) -> dict[str, Any]:
    requested = _redacted_mutation(mutation)
    if control_snapshot is None or treatment_snapshot is None:
        return {
            "mutation_requested": requested,
            "control_wire_value": None,
            "treatment_wire_value": None,
            "target_delta_observed": False,
            "non_target_fields_equivalent": False,
            "volatile_bindings_effective": False,
            "mutation_effective": False,
            "reason": "control and treatment exact wire snapshots are required",
        }
    try:
        control_exists, control_value = _observe_mutation_target(
            control_snapshot,
            mutation,
        )
        treatment_exists, treatment_value = _observe_mutation_target(
            treatment_snapshot,
            mutation,
        )
        control_count = (
            len(control_value) if isinstance(control_value, list) else 1 if control_exists else 0
        )
        treatment_count = (
            len(treatment_value)
            if isinstance(treatment_value, list)
            else 1
            if treatment_exists
            else 0
        )
        if mutation.type.startswith("remove_"):
            target_delta = control_exists and not treatment_exists
        elif mutation.type.startswith("add_"):
            expected = mutation.value
            target_delta = (
                treatment_exists
                and treatment_value != control_value
                and _occurrence_value_matches(
                    treatment_value if isinstance(treatment_value, list) else [treatment_value],
                    expected,
                    mutation.occurrence if hasattr(mutation, "occurrence") else 0,
                )
            )
        else:
            expected_treatment = (
                [mutation.value]
                if mutation.type in {"replace_header", "replace_query_parameter"}
                else mutation.value
            )
            target_delta = (
                control_exists
                and treatment_exists
                and control_value != treatment_value
                and treatment_value == expected_treatment
            )
        control_bindings_ok = _bindings_match_snapshot(
            control_snapshot,
            volatile_bindings,
            control_binding_values,
        )
        treatment_bindings_ok = _bindings_match_snapshot(
            treatment_snapshot,
            volatile_bindings,
            treatment_binding_values,
            mutation_target=mutation,
        )
        control_view = _canonical_pair_view(
            control_snapshot,
            volatile_bindings,
            mutation,
            role="control",
            normalize_order=normalize_wire_order,
        )
        treatment_view = _canonical_pair_view(
            treatment_snapshot,
            volatile_bindings,
            mutation,
            role="treatment",
            normalize_order=normalize_wire_order,
        )
        control_hash = canonical_json_sha256(control_view)
        treatment_hash = canonical_json_sha256(treatment_view)
        non_target_equivalent = control_hash == treatment_hash
        effective = bool(
            target_delta and control_bindings_ok and treatment_bindings_ok and non_target_equivalent
        )
        reasons: list[str] = []
        if not control_exists:
            reasons.append("control request did not contain the mutation target")
        if not target_delta:
            reasons.append("control/treatment target delta does not match the mutation")
        if not control_bindings_ok or not treatment_bindings_ok:
            reasons.append("one or more volatile bindings were not observed on wire")
        if not non_target_equivalent:
            reasons.append("non-target request fields differ after volatile normalization")
        return {
            "mutation_requested": requested,
            "control_wire_value": _public_target_value(
                control_exists,
                control_value,
                mutation,
            ),
            "treatment_wire_value": _public_target_value(
                treatment_exists,
                treatment_value,
                mutation,
            ),
            "target_delta_observed": target_delta,
            "control_value_count": control_count,
            "treatment_value_count": treatment_count,
            "multiplicity_changed": control_count != treatment_count,
            "non_target_fields_equivalent": non_target_equivalent,
            "volatile_bindings_effective": (control_bindings_ok and treatment_bindings_ok),
            "control_non_target_sha256": control_hash,
            "treatment_non_target_sha256": treatment_hash,
            "mutation_effective": effective,
            "reason": (
                "paired wire snapshots differ only by the requested mutation"
                if effective
                else "; ".join(reasons)
            ),
        }
    except ValueError as exc:
        return {
            "mutation_requested": requested,
            "control_wire_value": None,
            "treatment_wire_value": None,
            "target_delta_observed": False,
            "non_target_fields_equivalent": False,
            "volatile_bindings_effective": False,
            "mutation_effective": False,
            "reason": str(exc),
        }


def assess_control_wire_baseline(
    wire_snapshot: dict[str, Any] | None,
    *,
    volatile_bindings: list[VolatileBinding],
    binding_values: dict[str, Any],
) -> dict[str, Any]:
    if wire_snapshot is None:
        return {
            "mutation_requested": None,
            "control_wire_value": None,
            "treatment_wire_value": None,
            "target_delta_observed": None,
            "non_target_fields_equivalent": None,
            "volatile_bindings_effective": False,
            "mutation_effective": None,
            "reason": "control exact wire snapshot is required",
        }
    bindings_effective = _bindings_match_snapshot(
        wire_snapshot,
        volatile_bindings,
        binding_values,
    )
    return {
        "mutation_requested": None,
        "control_wire_value": None,
        "treatment_wire_value": None,
        "target_delta_observed": None,
        "non_target_fields_equivalent": None,
        "volatile_bindings_effective": bindings_effective,
        "mutation_effective": None,
        "reason": (
            "control replay wire baseline and volatile bindings are confirmed"
            if bindings_effective
            else "one or more control volatile bindings were not observed on wire"
        ),
    }


def analyze_replay_response(
    *,
    status: int | None,
    content_type: str | None,
    response_value: Any,
    mutation: ReplayMutation | None,
    redirected: bool = False,
    final_url: str | None = None,
    source_url: str | None = None,
    source_content_type: str | None = None,
) -> dict[str, Any]:
    reference = (
        _mutation_reference_evidence(response_value, mutation)
        if mutation is not None
        else {"strength": "none", "semantic": "none", "signals_conflict": False}
    )
    contract_mismatch = bool(
        status is not None
        and 200 <= status < 400
        and source_content_type
        and (not content_type or source_content_type != content_type)
    )
    validation_like = bool(
        status in {400, 422}
        and reference.get("strength") == "strong_structured"
        and reference.get("semantic")
        in {"field_required", "not_required", "value_constraint", "conflicting"}
    )
    conflict_like = status == 409
    authentication_like = status in {401, 403}
    rate_limit_like = status == 429
    server_failure_like = isinstance(status, int) and status >= 500
    redirect_like = bool(redirected or (isinstance(status, int) and 300 <= status < 400))
    success_like = bool(
        isinstance(status, int) and 200 <= status < 400 and not redirected and not contract_mismatch
    )
    if redirected:
        classification = "unexpected_redirect"
    elif contract_mismatch:
        classification = "response_contract_mismatch"
    elif status is None:
        classification = "unknown_response"
    elif authentication_like:
        classification = "authentication_failure"
    elif rate_limit_like:
        classification = "rate_limited"
    elif server_failure_like:
        classification = "server_failure"
    elif redirect_like:
        classification = "redirect_or_cache_response"
    elif conflict_like:
        classification = "conflict"
    elif status in {400, 422}:
        semantic = reference.get("semantic")
        classification = (
            "validation_rejection"
            if validation_like and semantic == "field_required"
            else "value_constraint"
            if validation_like and semantic == "value_constraint"
            else "field_rejection"
            if validation_like
            else "unknown_rejection"
        )
    elif status >= 400:
        classification = "unknown_rejection"
    else:
        classification = "success"
    matches = reference.get("matches") if isinstance(reference, dict) else None
    raw_paths = [
        item.get("raw_path")
        for item in matches or []
        if isinstance(item, dict) and item.get("raw_path") is not None
    ]
    normalized_paths = [
        item.get("normalized_path")
        for item in matches or []
        if isinstance(item, dict) and item.get("normalized_path") is not None
    ]
    observations = {
        "http_status": status,
        "response_content_type": content_type,
        "mutation_effective": None,
        "target_reference_strength": reference.get("strength"),
        "raw_validation_paths": raw_paths,
        "normalized_validation_paths": normalized_paths,
        "validation_like": validation_like,
        "conflict_like": conflict_like,
        "authentication_like": authentication_like,
        "rate_limit_like": rate_limit_like,
        "server_failure_like": server_failure_like,
        "redirect_like": redirect_like,
        "success_like": success_like,
        "signals_conflict": bool(reference.get("signals_conflict")),
    }
    hints: list[str] = []
    semantic = reference.get("semantic")
    if validation_like and semantic and semantic != "none":
        hints.append(str(semantic))
    if success_like and mutation is not None:
        hints.append("mutation_accepted_by_response")
    if observations["signals_conflict"]:
        hints.append("conflicting_validation_signals")
    return {
        "analyzer": {
            "name": "http_response_classifier",
            "version": "1",
        },
        "classification": classification,
        "validation_evidence": reference,
        "observations": observations,
        "hints": hints,
        "status": status,
        "content_type": content_type,
        "source_content_type": source_content_type,
        "final_url": final_url,
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


def _public_target_value(
    exists: bool,
    value: Any,
    mutation: ReplayMutation,
) -> Any:
    if not exists:
        return "<absent>"
    hint = getattr(mutation, "name", None) or _last_pointer_token(
        str(getattr(mutation, "path", ""))
    )
    return _redact_json_value(value, key_hint=hint)


def _bindings_match_snapshot(
    snapshot: dict[str, Any],
    bindings: list[VolatileBinding],
    expected_values: dict[str, Any],
    *,
    mutation_target: ReplayMutation | None = None,
) -> bool:
    for binding in bindings:
        if mutation_target is not None and _binding_targets_mutation(
            binding,
            mutation_target,
        ):
            continue
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


def _binding_targets_mutation(
    binding: VolatileBinding,
    mutation: ReplayMutation,
) -> bool:
    if mutation.type in {
        "remove_json_path",
        "replace_json_path",
        "add_json_path",
    }:
        if binding.target != "json_pointer":
            return False
        binding_tokens = _decode_pointer(str(binding.path))
        mutation_tokens = _decode_pointer(mutation.path)
        return (
            len(mutation_tokens) <= len(binding_tokens)
            and binding_tokens[: len(mutation_tokens)] == mutation_tokens
        )
    if mutation.type in {"remove_header", "replace_header", "add_header"}:
        return (
            binding.target == "header"
            and str(binding.name).lower() == mutation.name.lower()
            and (
                mutation.occurrence in {"all", "append"}
                or binding.occurrence == mutation.occurrence
            )
        )
    return (
        binding.target == "query_parameter"
        and binding.name == mutation.name
        and (mutation.occurrence in {"all", "append"} or binding.occurrence == mutation.occurrence)
    )


def _canonical_pair_view(
    snapshot: dict[str, Any],
    bindings: list[VolatileBinding],
    mutation: ReplayMutation,
    *,
    role: str,
    normalize_order: bool = False,
) -> dict[str, Any]:
    split = urlsplit(str(snapshot.get("url", "")))
    query = list(parse_qsl(split.query, keep_blank_values=True))
    headers = [
        {"name": item["name"].lower(), "value": item["value"]}
        for item in _normalized_headers(snapshot.get("requestHeadersArray"))
        if item["name"].lower() not in _BROWSER_MANAGED_HEADERS
        and not item["name"].lower().startswith("sec-")
    ]
    body = _decode_json_request_body(snapshot.get("requestBody"))
    body_descriptor: Any = body
    if body is None:
        body_descriptor = _non_json_body_descriptor(snapshot.get("requestBody"))
    for binding in bindings:
        placeholder = f"<volatile:{binding.binding_id}>"
        if binding.target == "json_pointer" and body is not None:
            exists, _ = _read_pointer(body, str(binding.path))
            if exists:
                _replace_pointer(body, str(binding.path), placeholder)
        elif binding.target == "header":
            for item in headers:
                if item["name"] == str(binding.name).lower():
                    item["value"] = placeholder
        else:
            query = [
                (name, placeholder if name == binding.name else value) for name, value in query
            ]
    if (
        mutation.type
        in {
            "remove_json_path",
            "replace_json_path",
            "add_json_path",
        }
        and body is not None
    ):
        should_remove_target = mutation.type != "remove_json_path" or role == "control"
        if should_remove_target:
            exists, _ = _read_pointer(body, mutation.path)
            if exists:
                _remove_pointer(body, mutation.path)
    elif mutation.type in {"remove_header", "replace_header", "add_header"}:
        if mutation.type != "add_header" or role == "treatment":
            headers = _remove_named_occurrence(
                headers,
                mutation.name,
                mutation.occurrence,
                case_sensitive=False,
            )
    else:
        if mutation.type != "add_query_parameter" or role == "treatment":
            query = _remove_named_occurrence(
                query,
                mutation.name,
                mutation.occurrence,
                case_sensitive=True,
            )
    if normalize_order:
        query = sorted(query)
        headers = sorted(headers, key=lambda item: (item["name"], item["value"]))
    return {
        "method": str(snapshot.get("method", "GET")).upper(),
        "url": urlunsplit((split.scheme, split.netloc, split.path, "", "")),
        "query": query,
        "headers": headers,
        "body": body_descriptor,
    }


def _non_json_body_descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value.get("available"):
        raise ValueError("wire request body is unavailable for non-target comparison")
    encoding = str(value.get("encoding", ""))
    size = value.get("size")
    if encoding == "utf8":
        payload = str(value.get("text", "")).encode("utf-8")
    elif encoding == "base64":
        encoded = str(value.get("base64") or value.get("text") or "")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("wire request base64 body is invalid") from exc
    else:
        digest = value.get("sha256") or value.get("encodedSha256")
        if not digest:
            raise ValueError("wire request body encoding cannot be compared")
        return {"kind": "non_json", "encoding": encoding, "size": size, "sha256": digest}
    return {
        "kind": "non_json",
        "encoding": encoding,
        "size": len(payload) if size is None else size,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def _mutation_reference_evidence(
    response_value: Any,
    mutation: ReplayMutation,
) -> dict[str, Any]:
    target_tokens = (
        _decode_pointer(mutation.path)
        if mutation.type
        in {
            "remove_json_path",
            "replace_json_path",
            "add_json_path",
        }
        else [mutation.name]
    )
    case_sensitive = mutation.type not in {
        "remove_header",
        "replace_header",
        "add_header",
    }
    structured = _structured_validation_references(response_value)
    matches: list[dict[str, Any]] = []
    for item in structured:
        path_tokens = item.get("path_tokens")
        if isinstance(path_tokens, list) and _validation_path_matches(
            path_tokens,
            target_tokens,
            case_sensitive=case_sensitive,
        ):
            code = _normalize_validation_code(item.get("code"))
            source_key = str(item.get("source_key") or "").lower()
            if source_key == "missing" or code in {
                "field_required",
                "missing",
                "value_error.missing",
            }:
                semantic = "field_required"
            elif source_key in {"optional", "not_required"} or code in {
                "not_required",
                "field_not_required",
                "extra_forbidden",
                "value_error.extra",
            }:
                semantic = "not_required"
            elif code in {
                "enum",
                "invalid_enum",
                "value_error.enum",
                "type_error",
                "value_error.type",
                "format_error",
                "value_error.format",
                "invalid_type",
                "invalid_format",
            }:
                semantic = "value_constraint"
            else:
                semantic = "field_reference"
            matches.append(
                {
                    "semantic": semantic,
                    "raw_path": item.get("raw_path"),
                    "normalized_path": item.get("normalized_path"),
                    "normalization_applied": item.get("normalization_applied", False),
                    "validation_code": item.get("code"),
                    "source_key": item.get("source_key"),
                }
            )
    if matches:
        semantics = {str(item["semantic"]) for item in matches}
        signals_conflict = "field_required" in semantics and "not_required" in semantics
        semantic = (
            "conflicting"
            if signals_conflict
            else next(iter(semantics))
            if len(semantics) == 1
            else "field_reference"
        )
        return {
            "strength": "strong_structured",
            "semantic": semantic,
            "signals_conflict": signals_conflict,
            "matches": matches,
        }
    strings = list(_validation_strings(response_value))
    if mutation.type in {
        "remove_json_path",
        "replace_json_path",
        "add_json_path",
    }:
        pointer = mutation.path
        token = target_tokens[-1] if target_tokens else ""
        candidates = [pointer, token]
    else:
        candidates = [mutation.name]
    for text in strings:
        for candidate in candidates:
            if candidate and re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])",
                text,
                flags=0 if case_sensitive else re.IGNORECASE,
            ):
                return {
                    "strength": "weak_text_match",
                    "semantic": "field_reference",
                    "matched_text": candidate,
                    "signals_conflict": False,
                }
    return {"strength": "none", "semantic": "none", "signals_conflict": False}


def _structured_validation_references(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def walk(item: Any, *, parent_key: str | None = None, code: str | None = None) -> None:
        if isinstance(item, dict):
            local_code = code
            for code_key in ("code", "type", "error_code", "kind"):
                candidate = item.get(code_key)
                if isinstance(candidate, str):
                    local_code = candidate
                    break
            for key, child in item.items():
                normalized = str(key).lower()
                if normalized in {
                    "field",
                    "path",
                    "loc",
                    "missing",
                    "fields",
                    "optional",
                    "not_required",
                }:
                    raw_paths = (
                        child
                        if isinstance(child, list) and normalized in {"missing", "fields"}
                        else [child]
                    )
                    for raw_path in raw_paths:
                        details = _validation_path_details(raw_path)
                        tokens = details["tokens"]
                        if tokens:
                            result.append(
                                {
                                    "path_tokens": tokens,
                                    "raw_path": raw_path,
                                    "normalized_path": details["normalized_path"],
                                    "normalization_applied": details["normalization_applied"],
                                    "source_key": normalized,
                                    "code": local_code,
                                }
                            )
                walk(child, parent_key=normalized, code=local_code)
        elif isinstance(item, list):
            for child in item:
                walk(child, parent_key=parent_key, code=code)
        elif isinstance(item, str):
            try:
                decoded = json.loads(item)
            except json.JSONDecodeError:
                return
            walk(decoded, parent_key=parent_key, code=code)

    walk(value)
    return result


def _validation_path_details(value: Any) -> dict[str, Any]:
    normalization_applied = False
    if isinstance(value, list):
        tokens = [str(item) for item in value]
        path_kind = "framework_location"
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("/"):
            try:
                tokens = _decode_pointer(text)
            except ValueError:
                return {
                    "tokens": [],
                    "normalized_path": None,
                    "normalization_applied": False,
                }
            path_kind = "json_pointer"
        else:
            tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_-]*|\d+", text)
            path_kind = "framework_location"
    else:
        return {
            "tokens": [],
            "normalized_path": None,
            "normalization_applied": False,
        }
    if path_kind == "framework_location":
        while tokens and tokens[0].lower() in {"body", "request", "payload", "json"}:
            tokens.pop(0)
            normalization_applied = True
    normalized_path = "/" + "/".join(_encode_pointer_token(item) for item in tokens)
    return {
        "tokens": tokens,
        "normalized_path": normalized_path if tokens else None,
        "normalization_applied": normalization_applied,
    }


def _normalize_validation_code(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _validation_path_matches(
    observed: list[str],
    expected: list[str],
    *,
    case_sensitive: bool,
) -> bool:
    observed_values = [str(item) for item in observed]
    expected_values = [str(item) for item in expected]
    if case_sensitive:
        return observed_values == expected_values
    return [item.lower() for item in observed_values] == [item.lower() for item in expected_values]


def _validation_strings(value: Any, *, key: str | None = None) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for child_key, child in value.items():
            result.extend(_validation_strings(child, key=str(child_key)))
    elif isinstance(value, list):
        for child in value:
            result.extend(_validation_strings(child, key=key))
    elif isinstance(value, str):
        if (key or "").lower() in {
            "bodypreview",
            "detail",
            "error",
            "errors",
            "field",
            "fields",
            "message",
            "missing",
            "path",
        }:
            result.append(value)
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = None
        if decoded is not None:
            result.extend(_validation_strings(decoded, key=key))
    return result


def _redacted_mutation(mutation: ReplayMutation) -> dict[str, Any]:
    value = mutation.model_dump(mode="json")
    if "value" in value:
        value["value"] = _redact_json_value(
            value["value"],
            key_hint=(value.get("name") or _last_pointer_token(str(value.get("path", "")))),
        )
    return value


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
) -> str:
    split = urlsplit(url)
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


def _decode_pointer(path: str) -> list[str]:
    if not path.startswith("/") or path == "/":
        raise ValueError(f"Invalid JSON Pointer: {path}")
    return [token.replace("~1", "/").replace("~0", "~") for token in path.split("/")[1:]]


def _encode_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _last_pointer_token(path: str) -> str | None:
    try:
        return _decode_pointer(path)[-1]
    except (ValueError, IndexError):
        return None


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
