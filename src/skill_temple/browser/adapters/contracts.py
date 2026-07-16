"""Typed contracts for Playwright, MCP, and js-reverse boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from ...browser_models import (
    EventPredicate,
    FlowStep,
    NetworkExportPart,
    RequestMatcher,
    WaitCondition,
)


class AdapterError(RuntimeError):
    """Raised when a private browser adapter cannot complete an operation."""

    def __init__(
        self,
        message: str,
        *,
        dispatch_started: bool = False,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.dispatch_started = dispatch_started
        self.outcome_unknown = outcome_unknown

class McpToolCallError(AdapterError):
    def __init__(
        self,
        message: str,
        *,
        outcome_unknown: bool,
        dispatch_started: bool,
        transport_generation: int,
    ) -> None:
        super().__init__(
            message,
            dispatch_started=dispatch_started,
            outcome_unknown=outcome_unknown,
        )
        self.transport_generation = transport_generation

class McpTransportError(AdapterError):
    pass

class DeadlineLike(Protocol):
    def remaining_seconds(self) -> float: ...

    def ensure_remaining(self, operation: str) -> None: ...

@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool = False

@dataclass(slots=True)
class PageState:
    url: str
    title: str = ""
    page_index: int = 0
    snapshot_ref: str | None = None

@dataclass(slots=True)
class AlignmentResult:
    status: str
    playwright_page: PageState
    js_reverse_page_index: int | None = None
    js_reverse_page_id: str | None = None
    js_reverse_page_url: str | None = None
    warnings: list[str] = field(default_factory=list)

@dataclass(slots=True)
class StreamRequestCheckpoint:
    response_observed: bool = False
    status: str | None = None
    terminal_wall_time_ms: float | None = None
    raw_event_index: int = -1
    semantic_event_index: int = -1
    primary_event_source: str = "none"

@dataclass(slots=True)
class StreamCheckpoint:
    version: int = 0
    requests: dict[str, StreamRequestCheckpoint] = field(default_factory=dict)

@dataclass(slots=True)
class StreamWaitResult:
    condition_met: bool
    capture_id: int
    capture_version: int
    matched_request_ids: list[str]
    terminal_status: str | None = None
    matched_event: dict[str, Any] | None = None
    checkpoint: StreamCheckpoint = field(default_factory=StreamCheckpoint)
    status_payload: dict[str, Any] = field(default_factory=dict)

class CommandRunner(Protocol):
    async def run(
        self,
        argv: list[str],
        *,
        deadline: DeadlineLike,
        cwd: Path | None = None,
        allow_failure: bool = False,
    ) -> CommandResult: ...

class PlaywrightAdapter(Protocol):
    async def open_session(
        self,
        session_ref: str,
        browser_endpoint: str,
        start_url: str | None,
        deadline: DeadlineLike,
    ) -> PageState: ...

    async def current_page(self, session_ref: str, deadline: DeadlineLike) -> PageState: ...

    async def select_page(
        self,
        session_ref: str,
        page_index: int,
        deadline: DeadlineLike,
    ) -> PageState: ...

    async def execute_step(
        self,
        session_ref: str,
        step: FlowStep,
        experiment_dir: Path,
        deadline: DeadlineLike,
    ) -> dict[str, Any]: ...

    async def wait_for_page_condition(
        self,
        session_ref: str,
        condition: WaitCondition,
        deadline: DeadlineLike,
    ) -> dict[str, Any]: ...

    async def start_trace(self, session_ref: str, deadline: DeadlineLike) -> None: ...

    async def stop_trace(
        self,
        session_ref: str,
        experiment_dir: Path,
        deadline: DeadlineLike,
        *,
        collect_files: bool = True,
    ) -> list[str]: ...

    async def capture_screenshot(
        self,
        session_ref: str,
        experiment_dir: Path,
        name: str,
        deadline: DeadlineLike,
    ) -> str: ...

    async def capture_snapshot(
        self,
        session_ref: str,
        experiment_dir: Path,
        name: str,
        deadline: DeadlineLike,
    ) -> str: ...

    async def close_session(self, session_ref: str, deadline: DeadlineLike) -> None: ...

class McpToolTransport(Protocol):
    @property
    def generation(self) -> int: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any], deadline: DeadlineLike
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...

class JsReverseAdapter(Protocol):
    @property
    def transport_generation(self) -> int: ...

    async def align_page(
        self,
        page: PageState,
        deadline: DeadlineLike,
        page_id: str | None = None,
    ) -> AlignmentResult: ...

    async def start_stream_capture(
        self,
        *,
        experiment_id: str,
        matcher: RequestMatcher,
        include_in_flight: bool,
        deadline: DeadlineLike,
    ) -> dict[str, Any]: ...

    async def get_stream_status(
        self,
        capture_id: int,
        deadline: DeadlineLike,
        *,
        request_id: str | None = None,
        event_predicate: EventPredicate | None = None,
        after_event_index: int = -1,
        event_source: Literal["raw-stream", "eventsource"] | None = None,
    ) -> dict[str, Any]: ...

    async def list_network_requests(
        self,
        matcher: RequestMatcher,
        deadline: DeadlineLike,
    ) -> dict[str, Any]: ...

    async def export_network_request(
        self,
        reqid: int,
        output_file: Path,
        output_part: NetworkExportPart,
        deadline: DeadlineLike,
    ) -> dict[str, Any]: ...

    async def get_request_initiator(self, reqid: int, deadline: DeadlineLike) -> dict[str, Any]: ...

    async def search_scripts(
        self,
        query: str,
        deadline: DeadlineLike,
        *,
        url_filter: str | None = None,
        max_results: int = 30,
        exclude_minified: bool = False,
    ) -> dict[str, Any]: ...

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
    ) -> dict[str, Any]: ...

    async def list_console_messages(
        self,
        deadline: DeadlineLike,
        *,
        types: list[str] | None = None,
        include_preserved_messages: bool = False,
    ) -> dict[str, Any]: ...

    async def trace_cookie_provenance(
        self, cookie_name: str, deadline: DeadlineLike
    ) -> dict[str, Any]: ...

    async def evaluate_browser_replay(
        self,
        spec_file: Path,
        output_file: Path,
        deadline: DeadlineLike,
    ) -> dict[str, Any]: ...

    async def wait_for_stream_condition(
        self,
        *,
        capture_id: int,
        request_matcher: RequestMatcher,
        condition: WaitCondition,
        checkpoint: StreamCheckpoint,
        deadline: DeadlineLike,
    ) -> StreamWaitResult: ...

    async def stop_stream_capture(
        self, capture_id: int, deadline: DeadlineLike
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...
