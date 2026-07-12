"""Pure helpers for protocol evidence indexing and structured request replay."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .browser_models import NetworkEvidenceSelector, ReplayMutation, RequestMatcher

_SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}
_SENSITIVE_HEADER_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "api-key",
    "apikey",
    "session",
    "csrf",
    "xsrf",
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
    reqids = sorted(
        int(item["reqid"])
        for item in requests
        if isinstance(item.get("reqid"), int)
    )
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
        int(item)
        for item in checkpoint.get("in_flight_reqids", [])
        if isinstance(item, int)
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
    return [
        item
        for item in requests
        if network_request_matches(item, selector.matcher)
    ][: selector.max_matches]


def is_sensitive_header(name: str) -> bool:
    normalized = name.lower()
    return normalized in _SENSITIVE_HEADER_NAMES or any(
        fragment in normalized for fragment in _SENSITIVE_HEADER_FRAGMENTS
    )


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
        "response_body": _public_body_summary(snapshot.get("responseBody")),
        "observed_at": snapshot.get("observedAt"),
        "timing": snapshot.get("timing"),
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


def build_replay_spec(
    snapshot: dict[str, Any],
    mutations: list[ReplayMutation],
) -> tuple[dict[str, Any], dict[str, Any]]:
    url = str(snapshot.get("url", ""))
    method = str(snapshot.get("method", "GET")).upper()
    headers = _normalized_headers(snapshot.get("requestHeadersArray"))
    body = copy.deepcopy(snapshot.get("requestBody"))
    ignored_headers: list[str] = []

    for mutation in mutations:
        if mutation.type == "remove_header":
            headers = [
                item
                for item in headers
                if item["name"].lower() != mutation.name.lower()
            ]
        elif mutation.type == "replace_header":
            headers = [
                item
                for item in headers
                if item["name"].lower() != mutation.name.lower()
            ]
            headers.append({"name": mutation.name, "value": mutation.value})
        elif mutation.type == "remove_query_parameter":
            url = _mutate_query(url, mutation.name, None, remove=True)
        elif mutation.type == "replace_query_parameter":
            url = _mutate_query(url, mutation.name, mutation.value, remove=False)
        elif mutation.type in {"remove_json_path", "replace_json_path"}:
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
            "url": str(snapshot.get("url", ""))[:8192],
            "method": method,
            "header_names": [item["name"] for item in headers],
            "request_body": _public_body_summary(snapshot.get("requestBody")),
        },
        "replay": {
            "url": url[:8192],
            "method": method,
            "header_names": [item["name"] for item in replay_headers],
            "ignored_browser_managed_headers": sorted(set(ignored_headers)),
            "request_body": _public_body_summary(body),
        },
        "mutations": [mutation.model_dump(mode="json") for mutation in mutations],
    }
    return spec, diff


def _normalized_headers(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"name": str(item.get("name", "")), "value": str(item.get("value", ""))}
        for item in value
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    ]


def _mutate_query(url: str, name: str, value: str | None, *, remove: bool) -> str:
    split = urlsplit(url)
    entries = [
        (key, item)
        for key, item in parse_qsl(split.query, keep_blank_values=True)
        if key != name
    ]
    if not remove:
        entries.append((name, value or ""))
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
    if not isinstance(value, dict):
        raise ValueError("JSON mutation currently requires an object request body")
    path = str(getattr(mutation, "path", ""))[2:].split(".")
    parent: Any = value
    for segment in path[:-1]:
        if not isinstance(parent, dict) or segment not in parent:
            raise ValueError(f"JSON path does not exist: $.{'.'.join(path)}")
        parent = parent[segment]
    if not isinstance(parent, dict):
        raise ValueError(f"JSON path parent is not an object: $.{'.'.join(path)}")
    leaf = path[-1]
    if mutation.type == "remove_json_path":
        if leaf not in parent:
            raise ValueError(f"JSON path does not exist: $.{'.'.join(path)}")
        del parent[leaf]
    else:
        parent[leaf] = mutation.value
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return {
        "available": True,
        "size": len(encoded.encode("utf-8")),
        "encoding": "utf8",
        "text": encoded,
    }
