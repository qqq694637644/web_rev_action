"""Explicit browser transport contracts and implementations."""

from .command import SubprocessCommandRunner
from .contracts import (
    AdapterError,
    AlignmentResult,
    CommandResult,
    CommandRunner,
    DeadlineLike,
    JsReverseAdapter,
    McpToolCallError,
    McpToolTransport,
    McpTransportError,
    PageState,
    PlaywrightAdapter,
    StreamCheckpoint,
    StreamRequestCheckpoint,
    StreamWaitResult,
)
from .js_reverse import JsReverseMcpAdapter
from .mcp import StdioMcpToolTransport
from .playwright import PlaywrightCliAdapter, build_playwright_attach_args

__all__ = [
    "AdapterError",
    "AlignmentResult",
    "CommandResult",
    "CommandRunner",
    "DeadlineLike",
    "JsReverseAdapter",
    "JsReverseMcpAdapter",
    "McpToolCallError",
    "McpToolTransport",
    "McpTransportError",
    "PageState",
    "PlaywrightAdapter",
    "PlaywrightCliAdapter",
    "StdioMcpToolTransport",
    "StreamCheckpoint",
    "StreamRequestCheckpoint",
    "StreamWaitResult",
    "SubprocessCommandRunner",
    "build_playwright_attach_args",
]
