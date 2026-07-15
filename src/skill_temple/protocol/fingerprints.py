"""Canonical protocol fingerprints used by replay and evidence comparison."""

from __future__ import annotations

import hashlib
import json
from typing import Any


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
