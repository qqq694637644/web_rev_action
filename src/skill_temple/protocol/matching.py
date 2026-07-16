"""Pure request matching and capture checkpoint selection."""

from __future__ import annotations

from typing import Any

from ..browser_models import NetworkEvidenceSelector, RequestMatcher


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
    if matcher.mime_types:
        mime_type = str(request.get("mimeType", "")).split(";", 1)[0].strip().lower()
        if mime_type not in {item.lower() for item in matcher.mime_types}:
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
