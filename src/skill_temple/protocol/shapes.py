"""Request body decoding, redaction, and structural shape helpers."""

from __future__ import annotations

import json
from typing import Any

from .values import _encode_pointer_token, _redact_json_value, _redact_scalar


def public_body_summary(value: Any) -> dict[str, Any] | None:
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

def redacted_request_body_from_snapshot(snapshot: dict[str, Any]) -> Any | None:
    body = decode_json_request_body(snapshot.get("requestBody"))
    if body is None:
        return None
    return _redact_json_value(body, key_hint=None)

def request_shape_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    body = decode_json_request_body(snapshot.get("requestBody"))
    if body is None:
        return None
    paths: dict[str, dict[str, Any]] = {}
    _collect_shape(body, "", paths, key_hint=None)
    return {"format": "json-pointer-v1", "paths": paths}

def decode_json_request_body(value: Any) -> Any | None:
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
