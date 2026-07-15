"""Advisory HTTP response analyzer with no execution side effects."""

from __future__ import annotations

import json
import re
from typing import Any

from ...browser_models import ReplayMutation
from ..mutations import _decode_pointer, _encode_pointer_token


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
