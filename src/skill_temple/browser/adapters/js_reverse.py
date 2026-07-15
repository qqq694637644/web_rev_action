"""js-reverse MCP tool mapping and stream operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

from ...browser_models import (
    EventPredicate,
    ExactDataPredicate,
    NetworkExportPart,
    RequestMatcher,
    WaitCondition,
)
from ..replay_runtime import load_replay_runtime
from .contracts import (
    AdapterError,
    AlignmentResult,
    DeadlineLike,
    McpToolTransport,
    PageState,
    StreamCheckpoint,
    StreamRequestCheckpoint,
    StreamWaitResult,
)


class JsReverseMcpAdapter:
    ALLOWED_TOOLS = frozenset(
        {
            "select_page",
            "select_frame",
            "list_network_requests",
            "get_request_initiator",
            "search_in_sources",
            "get_script_source",
            "evaluate_script",
            "list_console_messages",
            "break_on_xhr",
            "get_paused_info",
            "pause_or_resume",
            "start_stream_capture",
            "get_stream_status",
            "stop_stream_capture",
            "get_websocket_messages",
        }
    )

    def __init__(self, transport: McpToolTransport) -> None:
        self.transport = transport

    @property
    def transport_generation(self) -> int:
        return int(getattr(self.transport, "generation", 0))

    async def _call(
        self, name: str, arguments: dict[str, Any], deadline: DeadlineLike
    ) -> dict[str, Any]:
        if name not in self.ALLOWED_TOOLS:
            raise AdapterError(f"MCP tool is not in the private adapter allowlist: {name}")
        return await self.transport.call_tool(name, arguments, deadline)

    async def align_page(
        self,
        page: PageState,
        deadline: DeadlineLike,
        page_id: str | None = None,
    ) -> AlignmentResult:
        listing = await self._call("select_page", {"pageSize": 100, "listPageIdx": 0}, deadline)
        pages = listing.get("pages") if isinstance(listing.get("pages"), list) else []
        if page_id:
            stable = [item for item in pages if str(item.get("pageId", "")) == page_id]
            if not stable:
                return AlignmentResult(
                    status="not_aligned",
                    playwright_page=page,
                    warnings=["The saved js-reverse pageId is no longer available."],
                )
            selected = stable[0]
            if str(selected.get("url", "")) != page.url:
                return AlignmentResult(
                    status="not_aligned",
                    playwright_page=page,
                    js_reverse_page_id=page_id,
                    warnings=["The saved pageId now points to a different URL."],
                )
            await self._call("select_page", {"pageId": page_id, "pageSize": 100}, deadline)
            return AlignmentResult(
                status="aligned",
                playwright_page=page,
                js_reverse_page_index=int(selected.get("pageIdx", 0)),
                js_reverse_page_id=page_id,
                js_reverse_page_url=str(selected.get("url", "")),
            )
        indexed = [
            item
            for item in pages
            if int(item.get("pageIdx", -1)) == page.page_index
            and str(item.get("url", "")) == page.url
        ]
        exact = [item for item in pages if str(item.get("url", "")) == page.url]
        candidates = (
            indexed
            or exact
            or [
                item
                for item in pages
                if page.url and page.url.rstrip("/") == str(item.get("url", "")).rstrip("/")
            ]
        )
        if not candidates:
            return AlignmentResult(
                status="not_aligned",
                playwright_page=page,
                warnings=["No js-reverse page matched the Playwright URL."],
            )
        selected = candidates[0]
        page_index = int(selected["pageIdx"])
        selected_page_id = str(selected.get("pageId", "")) or None
        selector = {"pageSize": 100}
        selector["pageId" if selected_page_id else "pageIdx"] = (
            selected_page_id if selected_page_id else page_index
        )
        await self._call("select_page", selector, deadline)
        return AlignmentResult(
            status="aligned",
            playwright_page=page,
            js_reverse_page_index=page_index,
            js_reverse_page_id=selected_page_id,
            js_reverse_page_url=str(selected.get("url", "")),
            warnings=(
                ["Multiple matching pages; selected the first."] if len(candidates) > 1 else []
            ),
        )

    async def start_stream_capture(
        self,
        *,
        experiment_id: str,
        matcher: RequestMatcher,
        include_in_flight: bool,
        deadline: DeadlineLike,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "artifactNamespace": experiment_id,
            "includeInFlight": include_in_flight,
        }
        if matcher.url_contains:
            arguments["urlFilter"] = matcher.url_contains
        if matcher.method:
            arguments["methods"] = [matcher.method]
        if matcher.resource_types:
            arguments["resourceTypes"] = matcher.resource_types
        if matcher.mime_types:
            arguments["mimeTypes"] = matcher.mime_types
        return await self._call("start_stream_capture", arguments, deadline)

    async def get_stream_status(
        self,
        capture_id: int,
        deadline: DeadlineLike,
        *,
        request_id: str | None = None,
        event_predicate: EventPredicate | None = None,
        after_event_index: int = -1,
        event_source: Literal["raw-stream", "eventsource"] | None = None,
    ) -> dict[str, Any]:
        def arguments_for(page_idx: int) -> dict[str, Any]:
            arguments: dict[str, Any] = {
                "captureId": capture_id,
                "pageIdx": page_idx,
                "pageSize": 100,
                "afterEventIndex": after_event_index,
            }
            if request_id:
                arguments["requestId"] = request_id
            if event_source:
                arguments["eventSource"] = event_source
            if event_predicate:
                if event_predicate.type == "exact_data":
                    arguments["eventPredicate"] = {
                        "type": "exact_data",
                        "value": str(event_predicate.value),
                    }
                elif event_predicate.type == "event_name":
                    arguments["eventPredicate"] = {
                        "type": "event_name",
                        "value": event_predicate.event_name or "",
                    }
                elif event_predicate.type == "json_path_equals":
                    arguments["eventPredicate"] = {
                        "type": "json_path_equals",
                        "path": event_predicate.path or "",
                        "value": event_predicate.value,
                    }
            return arguments

        if request_id:
            payload = await self._call(
                "get_stream_status",
                arguments_for(0),
                deadline,
            )
            request = payload.get("request")
            if isinstance(request, dict) and not isinstance(payload.get("requests"), list):
                payload = {**payload, "requests": [request]}
            return payload

        page_idx = 0
        combined: dict[str, Any] = {}
        requests: list[dict[str, Any]] = []
        while True:
            page = await self._call(
                "get_stream_status",
                arguments_for(page_idx),
                deadline,
            )
            if not combined:
                combined = {
                    key: value
                    for key, value in page.items()
                    if key not in {"requests", "pagination"}
                }
            page_requests = page.get("requests")
            if isinstance(page_requests, list):
                requests.extend(item for item in page_requests if isinstance(item, dict))
            pagination = page.get("pagination") if isinstance(page.get("pagination"), dict) else {}
            if not pagination.get("hasNextPage"):
                break
            page_idx += 1
            if page_idx >= int(pagination.get("totalPages", page_idx + 1)):
                break
        combined["requests"] = requests
        combined["pagination"] = {
            "pageIdx": 0,
            "pageSize": 100,
            "totalItems": len(requests),
            "totalPages": max(1, page_idx + 1),
            "hasNextPage": False,
            "hasPreviousPage": False,
        }
        return combined

    async def list_network_requests(
        self,
        matcher: RequestMatcher,
        deadline: DeadlineLike,
    ) -> dict[str, Any]:
        def arguments_for(page_idx: int) -> dict[str, Any]:
            arguments: dict[str, Any] = {"pageIdx": page_idx, "pageSize": 100}
            if matcher.url_contains:
                arguments["urlFilter"] = matcher.url_contains
            if matcher.method:
                arguments["methods"] = [matcher.method]
            if matcher.resource_types:
                arguments["resourceTypes"] = matcher.resource_types
            return arguments

        page_idx = 0
        combined: dict[str, Any] = {}
        requests: list[dict[str, Any]] = []
        while True:
            page = await self._call(
                "list_network_requests",
                arguments_for(page_idx),
                deadline,
            )
            if not combined:
                combined = {
                    key: value
                    for key, value in page.items()
                    if key not in {"requests", "pagination"}
                }
            page_requests = page.get("requests")
            if isinstance(page_requests, list):
                requests.extend(item for item in page_requests if isinstance(item, dict))
            pagination = page.get("pagination") if isinstance(page.get("pagination"), dict) else {}
            if not pagination.get("hasNextPage"):
                break
            page_idx += 1
            if page_idx >= int(pagination.get("totalPages", page_idx + 1)):
                break
        combined["requests"] = requests
        combined["pagination"] = {
            "pageIdx": 0,
            "pageSize": 100,
            "totalItems": len(requests),
            "totalPages": max(1, page_idx + 1),
            "hasNextPage": False,
            "hasPreviousPage": False,
        }
        return combined

    async def export_network_request(
        self,
        reqid: int,
        output_file: Path,
        output_part: NetworkExportPart,
        deadline: DeadlineLike,
    ) -> dict[str, Any]:
        return await self._call(
            "list_network_requests",
            {
                "reqid": reqid,
                "outputFile": str(output_file.resolve()),
                "outputPart": output_part,
                "confirmOverwrite": False,
            },
            deadline,
        )

    async def get_request_initiator(self, reqid: int, deadline: DeadlineLike) -> dict[str, Any]:
        return await self._call(
            "get_request_initiator",
            {"requestId": reqid},
            deadline,
        )

    async def search_scripts(
        self,
        query: str,
        deadline: DeadlineLike,
        *,
        url_filter: str | None = None,
        max_results: int = 30,
        exclude_minified: bool = False,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "query": query,
            "maxResults": max_results,
            "excludeMinified": exclude_minified,
        }
        if url_filter:
            arguments["urlFilter"] = url_filter
        return await self._call("search_in_sources", arguments, deadline)

    async def get_script_source(
        self,
        deadline: DeadlineLike,
        *,
        url: str | None = None,
        script_id: str | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        offset: int | None = None,
        length: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if url:
            arguments["url"] = url
        if script_id:
            arguments["scriptId"] = script_id
        if start_line is not None:
            arguments["startLine"] = start_line
        if end_line is not None:
            arguments["endLine"] = end_line
        if offset is not None:
            arguments["offset"] = offset
        if length is not None:
            arguments["length"] = length
        return await self._call("get_script_source", arguments, deadline)

    async def list_console_messages(
        self,
        deadline: DeadlineLike,
        *,
        types: list[str] | None = None,
        include_preserved_messages: bool = False,
    ) -> dict[str, Any]:
        page_idx = 0
        messages: list[dict[str, Any]] = []
        combined: dict[str, Any] = {}
        while True:
            arguments: dict[str, Any] = {
                "pageIdx": page_idx,
                "pageSize": 100,
                "includePreservedMessages": include_preserved_messages,
            }
            if types:
                arguments["types"] = types
            page = await self._call("list_console_messages", arguments, deadline)
            if not combined:
                combined = {
                    key: value
                    for key, value in page.items()
                    if key not in {"messages", "pagination"}
                }
            page_messages = page.get("messages")
            if isinstance(page_messages, list):
                messages.extend(item for item in page_messages if isinstance(item, dict))
            pagination = page.get("pagination") if isinstance(page.get("pagination"), dict) else {}
            if not pagination.get("hasNextPage"):
                break
            page_idx += 1
            if page_idx >= int(pagination.get("totalPages", page_idx + 1)):
                break
        combined["messages"] = messages
        combined["pagination"] = {
            "pageIdx": 0,
            "pageSize": 100,
            "totalItems": len(messages),
            "totalPages": max(1, page_idx + 1),
            "hasNextPage": False,
            "hasPreviousPage": False,
        }
        return combined

    async def trace_cookie_provenance(
        self, cookie_name: str, deadline: DeadlineLike
    ) -> dict[str, Any]:
        page_idx = 0
        entries: list[dict[str, Any]] = []
        while True:
            page = await self._call(
                "list_network_requests",
                {
                    "cookieName": cookie_name,
                    "pageIdx": page_idx,
                    "pageSize": 100,
                },
                deadline,
            )
            values = page.get("cookieFlow")
            if isinstance(values, list):
                entries.extend(item for item in values if isinstance(item, dict))
            pagination = page.get("pagination") if isinstance(page.get("pagination"), dict) else {}
            if not pagination.get("hasNextPage"):
                break
            page_idx += 1
            if page_idx >= int(pagination.get("totalPages", page_idx + 1)):
                break
        return {"cookieName": cookie_name, "cookieFlow": entries}

    async def evaluate_browser_replay(
        self,
        spec_file: Path,
        output_file: Path,
        deadline: DeadlineLike,
    ) -> dict[str, Any]:
        function = load_replay_runtime()
        return await self._call(
            "evaluate_script",
            {
                "confirm": True,
                "function": function,
                "mainWorld": True,
                "localFilePath": str(spec_file.resolve()),
                "outputFile": str(output_file.resolve()),
                "confirmOverwrite": False,
            },
            deadline,
        )

    @staticmethod
    def _request_matches(request: dict[str, Any], matcher: RequestMatcher) -> bool:
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

    @staticmethod
    def _request_id(request: dict[str, Any]) -> str:
        return str(request.get("cdpRequestId") or request.get("persistentRequestId") or "")

    @staticmethod
    def _request_checkpoint(request: dict[str, Any]) -> StreamRequestCheckpoint:
        ended = request.get("endedWallTimeMs")
        return StreamRequestCheckpoint(
            response_observed=bool(request.get("responseObserved")),
            status=(str(request.get("status")) if request.get("status") else None),
            terminal_wall_time_ms=(float(ended) if isinstance(ended, (int, float)) else None),
            raw_event_index=int(request.get("rawEventCount", 0) or 0) - 1,
            semantic_event_index=int(request.get("semanticEventCount", 0) or 0) - 1,
            primary_event_source=str(request.get("primaryEventSource") or "none"),
        )

    @classmethod
    def _event_match_belongs_to_request(
        cls,
        candidate: Any,
        request: dict[str, Any],
    ) -> bool:
        if not isinstance(candidate, dict) or not candidate.get("matched"):
            return False
        matched_request_id = str(candidate.get("matchedRequestId") or "")
        aliases = {
            str(request.get("cdpRequestId") or ""),
            str(request.get("persistentRequestId") or ""),
        }
        aliases.discard("")
        return bool(matched_request_id and matched_request_id in aliases)

    @classmethod
    def checkpoint_from_status(
        cls,
        payload: dict[str, Any],
        matcher: RequestMatcher,
    ) -> StreamCheckpoint:
        capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
        requests: dict[str, StreamRequestCheckpoint] = {}
        for request in payload.get("requests", []):
            if not isinstance(request, dict) or not cls._request_matches(request, matcher):
                continue
            request_id = cls._request_id(request)
            if request_id:
                requests[request_id] = cls._request_checkpoint(request)
        return StreamCheckpoint(
            version=int(capture.get("version", 0) or 0),
            requests=requests,
        )

    @staticmethod
    def _terminal_transition(
        current: StreamRequestCheckpoint,
        previous: StreamRequestCheckpoint | None,
        desired: set[str],
    ) -> bool:
        if current.status not in desired:
            return False
        if previous is None or previous.status != current.status:
            return True
        if current.terminal_wall_time_ms is None:
            return False
        return (
            previous.terminal_wall_time_ms is None
            or current.terminal_wall_time_ms > previous.terminal_wall_time_ms
        )

    @staticmethod
    def _advanced_event_sources(
        current: StreamRequestCheckpoint,
        previous: StreamRequestCheckpoint | None,
    ) -> list[tuple[Literal["raw-stream", "eventsource"], int]]:
        prior_raw = previous.raw_event_index if previous else -1
        prior_semantic = previous.semantic_event_index if previous else -1
        sources: list[tuple[Literal["raw-stream", "eventsource"], int]] = []
        if current.raw_event_index > prior_raw:
            sources.append(("raw-stream", prior_raw))
        if current.semantic_event_index > prior_semantic:
            sources.append(("eventsource", prior_semantic))
        return sources

    async def wait_for_stream_condition(
        self,
        *,
        capture_id: int,
        request_matcher: RequestMatcher,
        condition: WaitCondition,
        checkpoint: StreamCheckpoint,
        deadline: DeadlineLike,
    ) -> StreamWaitResult:
        last_payload: dict[str, Any] = {}
        while deadline.remaining_seconds() > 0:
            payload = await self.get_stream_status(
                capture_id,
                deadline,
                request_id=request_matcher.request_id,
            )
            last_payload = payload
            capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
            version = int(capture.get("version", 0) or 0)
            requests = [
                item
                for item in payload.get("requests", [])
                if isinstance(item, dict) and self._request_matches(item, request_matcher)
            ]
            current_checkpoint = self.checkpoint_from_status(payload, request_matcher)
            request_by_id = {
                request_id: item for item in requests if (request_id := self._request_id(item))
            }
            met = False
            matched_event: dict[str, Any] | None = None
            matched_request_ids: list[str] = []
            terminal_status: str | None = None
            if condition.type == "first_event":
                matched_request_ids = [
                    request_id
                    for request_id, current in current_checkpoint.requests.items()
                    if self._advanced_event_sources(
                        current,
                        checkpoint.requests.get(request_id),
                    )
                ]
                met = bool(matched_request_ids)
            elif condition.type == "default_done_marker":
                for request_id, request in request_by_id.items():
                    current = current_checkpoint.requests[request_id]
                    previous = checkpoint.requests.get(request_id)
                    for source, prior_index in self._advanced_event_sources(current, previous):
                        candidate_payload = await self.get_stream_status(
                            capture_id,
                            deadline,
                            request_id=request_id,
                            event_predicate=ExactDataPredicate(
                                type="exact_data",
                                value="[DONE]",
                            ),
                            after_event_index=prior_index,
                            event_source=source,
                        )
                        candidate = candidate_payload.get("eventMatch")
                        if self._event_match_belongs_to_request(candidate, request):
                            matched_event = candidate
                            matched_request_ids = [request_id]
                            met = True
                            last_payload = candidate_payload
                            break
                    if met:
                        break
            elif condition.type == "network_finished":
                matched_request_ids = [
                    request_id
                    for request_id, current in current_checkpoint.requests.items()
                    if self._terminal_transition(
                        current,
                        checkpoint.requests.get(request_id),
                        {"finished"},
                    )
                ]
                met = bool(matched_request_ids)
                terminal_status = "finished" if met else None
            elif condition.type == "network_canceled":
                matched_request_ids = [
                    request_id
                    for request_id, current in current_checkpoint.requests.items()
                    if self._terminal_transition(
                        current,
                        checkpoint.requests.get(request_id),
                        {"canceled"},
                    )
                ]
                met = bool(matched_request_ids)
                terminal_status = "canceled" if met else None
            elif condition.type == "failed":
                matched_request_ids = [
                    request_id
                    for request_id, current in current_checkpoint.requests.items()
                    if self._terminal_transition(
                        current,
                        checkpoint.requests.get(request_id),
                        {"failed"},
                    )
                ]
                met = bool(matched_request_ids)
                terminal_status = "failed" if met else None
            elif condition.type == "request_observed":
                matched_request_ids = [
                    request_id
                    for request_id in current_checkpoint.requests
                    if request_id not in checkpoint.requests
                ]
                met = bool(matched_request_ids)
            elif condition.type == "response_observed":
                matched_request_ids = [
                    request_id
                    for request_id, current in current_checkpoint.requests.items()
                    if current.response_observed
                    and not (
                        checkpoint.requests.get(request_id)
                        and checkpoint.requests[request_id].response_observed
                    )
                ]
                met = bool(matched_request_ids)
            elif condition.type == "event_predicate" and condition.predicate:
                if condition.predicate.type == "network_terminal":
                    desired = condition.predicate.value
                    desired_statuses = (
                        {str(desired)}
                        if desired is not None
                        else {"finished", "canceled", "failed", "stopped"}
                    )
                    matched_request_ids = [
                        request_id
                        for request_id, current in current_checkpoint.requests.items()
                        if self._terminal_transition(
                            current,
                            checkpoint.requests.get(request_id),
                            desired_statuses,
                        )
                    ]
                    met = bool(matched_request_ids)
                    statuses = {
                        current_checkpoint.requests[request_id].status
                        for request_id in matched_request_ids
                    }
                    terminal_status = next(iter(statuses)) if len(statuses) == 1 else None
                else:
                    for request_id, request in request_by_id.items():
                        current = current_checkpoint.requests[request_id]
                        previous = checkpoint.requests.get(request_id)
                        for source, prior_index in self._advanced_event_sources(current, previous):
                            candidate_payload = await self.get_stream_status(
                                capture_id,
                                deadline,
                                request_id=request_id,
                                event_predicate=condition.predicate,
                                after_event_index=prior_index,
                                event_source=source,
                            )
                            candidate = candidate_payload.get("eventMatch")
                            if self._event_match_belongs_to_request(candidate, request):
                                matched_event = candidate
                                matched_request_ids = [request_id]
                                met = True
                                last_payload = candidate_payload
                                break
                        if met:
                            break
            if met:
                return StreamWaitResult(
                    condition_met=True,
                    capture_id=capture_id,
                    capture_version=version,
                    matched_request_ids=matched_request_ids,
                    terminal_status=terminal_status,
                    matched_event=matched_event,
                    checkpoint=current_checkpoint,
                    status_payload=payload,
                )
            await asyncio.sleep(min(0.2, max(0.01, deadline.remaining_seconds())))
        return StreamWaitResult(
            condition_met=False,
            capture_id=capture_id,
            capture_version=int((last_payload.get("capture") or {}).get("version", 0) or 0),
            matched_request_ids=[],
            checkpoint=self.checkpoint_from_status(last_payload, request_matcher),
            status_payload=last_payload,
        )

    async def stop_stream_capture(self, capture_id: int, deadline: DeadlineLike) -> dict[str, Any]:
        remaining_ms = max(100, min(34_000, int(deadline.remaining_seconds() * 1000)))
        return await self._call(
            "stop_stream_capture",
            {"captureId": capture_id, "finalizeTimeoutMs": remaining_ms},
            deadline,
        )

    async def close(self) -> None:
        await self.transport.close()
