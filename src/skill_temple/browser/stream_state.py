"""Pure stream-status matching and checkpoint conversion."""

from __future__ import annotations

from typing import Any

from ..browser_models import RequestMatcher
from .adapters.contracts import StreamCheckpoint, StreamRequestCheckpoint


def request_matches_stream_request(
    request: dict[str, Any],
    matcher: RequestMatcher,
) -> bool:
    """Return whether one js-reverse stream request matches the public matcher."""
    if matcher.request_id and matcher.request_id not in {
        request.get("cdpRequestId"),
        request.get("persistentRequestId"),
    }:
        return False
    if matcher.url_contains and matcher.url_contains not in str(request.get("url", "")):
        return False
    if matcher.method and matcher.method != str(request.get("method", "")).upper():
        return False
    if matcher.resource_types and str(request.get("resourceType", "")).lower() not in {
        value.lower() for value in matcher.resource_types
    }:
        return False
    return True


def stream_request_id(request: dict[str, Any]) -> str:
    """Return the stable request identifier exposed by a stream status payload."""
    return str(request.get("cdpRequestId") or request.get("persistentRequestId") or "")


def stream_request_checkpoint(request: dict[str, Any]) -> StreamRequestCheckpoint:
    """Convert one stream request status into an immutable comparison checkpoint."""
    ended = request.get("endedWallTimeMs")
    return StreamRequestCheckpoint(
        response_observed=bool(request.get("responseObserved")),
        status=(str(request.get("status")) if request.get("status") else None),
        terminal_wall_time_ms=(
            float(ended) if isinstance(ended, (int, float)) else None
        ),
        raw_event_index=int(request.get("rawEventCount", 0) or 0) - 1,
        semantic_event_index=int(request.get("semanticEventCount", 0) or 0) - 1,
        primary_event_source=str(request.get("primaryEventSource") or "none"),
    )


def checkpoint_from_status(
    payload: dict[str, Any],
    matcher: RequestMatcher,
) -> StreamCheckpoint:
    """Build a checkpoint from matching requests in a js-reverse status payload."""
    capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
    requests: dict[str, StreamRequestCheckpoint] = {}
    for request in payload.get("requests", []):
        if not isinstance(request, dict) or not request_matches_stream_request(
            request,
            matcher,
        ):
            continue
        request_id = stream_request_id(request)
        if request_id:
            requests[request_id] = stream_request_checkpoint(request)
    return StreamCheckpoint(
        version=int(capture.get("version", 0) or 0),
        requests=requests,
    )
