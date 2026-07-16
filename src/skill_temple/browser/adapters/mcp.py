"""Persistent stdio MCP transport implementation."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from .contracts import (
    AdapterError,
    DeadlineLike,
    McpToolCallError,
    McpTransportError,
)


@dataclass(slots=True)
class _McpCall:
    name: str
    arguments: dict[str, Any]
    timeout_seconds: float
    absolute_deadline: float
    generation: int
    future: asyncio.Future[dict[str, Any]]
    sent: bool = False

class StdioMcpToolTransport:
    """Persistent MCP stdio client owned by one dedicated asyncio task."""

    SIDE_EFFECTING_TOOLS = frozenset(
        {
            "select_page",
            "select_frame",
            "break_on_xhr",
            "pause_or_resume",
            "start_stream_capture",
            "stop_stream_capture",
            "evaluate_script",
        }
    )

    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.args = args or []
        self.cwd = cwd
        self.env = env
        self._start_lock = asyncio.Lock()
        self._queue: asyncio.Queue[_McpCall | None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._worker_error: BaseException | None = None
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    @staticmethod
    def _is_transport_failure(exc: BaseException) -> bool:
        if isinstance(exc, McpTransportError):
            return True
        if isinstance(exc, BaseExceptionGroup):
            return any(StdioMcpToolTransport._is_transport_failure(item) for item in exc.exceptions)
        if isinstance(exc, (EOFError, BrokenPipeError, ConnectionResetError, OSError)):
            return True
        if exc.__class__.__name__ == "McpError":
            message = str(exc).lower()
            return any(
                marker in message
                for marker in (
                    "connection closed",
                    "stream closed",
                    "end of stream",
                    "eof",
                    "disconnected",
                )
            )
        return exc.__class__.__name__ in {
            "EndOfStream",
            "BrokenResourceError",
            "ClosedResourceError",
        }

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            stale_worker = self._worker_task is not None and (
                self._worker_task.done() or self._ready is None or self._ready.cancelled()
            )
            if stale_worker:
                if self._worker_task is not None and not self._worker_task.done():
                    self._worker_task.cancel()
                self._queue = None
                self._worker_task = None
                self._ready = None
                self._worker_error = None
            if self._worker_task is None:
                loop = asyncio.get_running_loop()
                self._generation += 1
                self._queue = asyncio.Queue()
                self._ready = loop.create_future()
                self._worker_error = None
                self._worker_task = asyncio.create_task(
                    self._run_worker(),
                    name="js-reverse-mcp-stdio-worker",
                )
            ready = self._ready
        if ready is None:
            raise AdapterError("MCP worker failed to initialize")
        await ready
        if self._worker_error is not None:
            raise AdapterError(f"MCP worker failed: {self._worker_error}")

    async def _run_worker(self) -> None:
        ready = self._ready
        queue = self._queue
        generation = self._generation
        if ready is None or queue is None:
            return
        try:
            try:
                from mcp import ClientSession, StdioServerParameters
                from mcp.client.stdio import stdio_client
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise AdapterError("Install the 'mcp' package to use js-reverse-mcp") from exc
            parameters = StdioServerParameters(
                command=self.command,
                args=self.args,
                cwd=str(self.cwd) if self.cwd else None,
                env={**os.environ, **(self.env or {})},
            )
            async with AsyncExitStack() as stack:
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(parameters)
                )
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                if not ready.done():
                    ready.set_result(None)
                while True:
                    call = await queue.get()
                    if call is None:
                        break
                    if (
                        call.future.cancelled()
                        or call.generation != self._generation
                        or asyncio.get_running_loop().time() >= call.absolute_deadline
                    ):
                        if not call.future.done():
                            call.future.cancel()
                        continue
                    try:
                        remaining = max(
                            0.1,
                            call.absolute_deadline - asyncio.get_running_loop().time(),
                        )
                        call.sent = True
                        result = await session.call_tool(
                            call.name,
                            call.arguments,
                            read_timeout_seconds=timedelta(
                                seconds=min(call.timeout_seconds, remaining)
                            ),
                        )
                        parsed = self._normalize_result(call.name, result)
                    except BaseException as exc:
                        if isinstance(exc, asyncio.CancelledError):
                            if not call.future.done():
                                call.future.cancel()
                            raise
                        transport_failure = self._is_transport_failure(exc) or (
                            not isinstance(exc, AdapterError)
                            and exc.__class__.__name__ != "McpError"
                        )
                        delivered: BaseException = exc
                        if transport_failure:
                            delivered = McpTransportError(
                                f"MCP transport failed during {call.name}: {exc}",
                                dispatch_started=call.sent,
                                outcome_unknown=(
                                    call.sent and call.name in self.SIDE_EFFECTING_TOOLS
                                ),
                            )
                            delivered.transport_generation = generation
                        elif call.name in self.SIDE_EFFECTING_TOOLS:
                            delivered = McpToolCallError(
                                f"MCP tool failed after dispatch: {call.name}: {exc}",
                                outcome_unknown=False,
                                dispatch_started=call.sent,
                                transport_generation=generation,
                            )
                        else:
                            delivered = AdapterError(
                                f"MCP tool failed after dispatch: {call.name}: {exc}",
                                dispatch_started=call.sent,
                                outcome_unknown=False,
                            )
                        if not call.future.done():
                            call.future.set_exception(delivered)
                        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                            raise
                        if transport_failure:
                            raise
                    else:
                        if not call.future.cancelled():
                            call.future.set_result(parsed)
        except BaseException as exc:
            if generation == self._generation:
                self._worker_error = exc
            if not ready.done():
                if isinstance(exc, asyncio.CancelledError):
                    ready.cancel()
                else:
                    ready.set_exception(exc)
            while not queue.empty():
                pending = queue.get_nowait()
                if pending is None or pending.future.done():
                    continue
                if isinstance(exc, asyncio.CancelledError):
                    pending.future.cancel()
                else:
                    pending.future.set_exception(exc)

    @staticmethod
    def _normalize_result(name: str, result: Any) -> dict[str, Any]:
        if getattr(result, "isError", False) or getattr(result, "is_error", False):
            raise AdapterError(f"MCP tool failed: {name}")
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            if structured.get("ok") is False:
                error = structured.get("error") or {}
                raise AdapterError(str(error.get("message") or f"MCP tool failed: {name}"))
            data = structured.get("data")
            return data if isinstance(data, dict) else structured
        for block in getattr(result, "content", []):
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed.get("data", parsed)
        return {}

    async def call_tool(
        self, name: str, arguments: dict[str, Any], deadline: DeadlineLike
    ) -> dict[str, Any]:
        deadline.ensure_remaining(name)
        await self._ensure_started()
        queue = self._queue
        task = self._worker_task
        if queue is None or task is None or task.done():
            error = self._worker_error or RuntimeError("MCP worker is not running")
            raise AdapterError(str(error))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        timeout_seconds = max(0.1, deadline.remaining_seconds())
        absolute_deadline = loop.time() + timeout_seconds
        call = _McpCall(
            name=name,
            arguments=arguments,
            timeout_seconds=timeout_seconds,
            absolute_deadline=absolute_deadline,
            generation=self._generation,
            future=future,
        )
        await queue.put(call)
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as exc:
            future.cancel()
            if name in self.SIDE_EFFECTING_TOOLS:
                await self._abort_worker()
            raise McpToolCallError(
                f"MCP tool timed out: {name}",
                outcome_unknown=(call.sent and name in self.SIDE_EFFECTING_TOOLS),
                dispatch_started=call.sent,
                transport_generation=call.generation,
            ) from exc
        except asyncio.CancelledError as exc:
            future.cancel()
            exc.mcp_outcome_unknown = call.sent
            exc.mcp_transport_generation = call.generation
            if name in self.SIDE_EFFECTING_TOOLS:
                cleanup = asyncio.create_task(self._abort_worker())
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
            raise
        except BaseException as exc:
            if self._is_transport_failure(exc):
                await self._abort_worker()
                if isinstance(exc, AdapterError):
                    raise
                error = McpTransportError(
                    f"MCP transport failed and was restarted: {name}: {exc}",
                    dispatch_started=call.sent,
                    outcome_unknown=(call.sent and name in self.SIDE_EFFECTING_TOOLS),
                )
                error.transport_generation = call.generation
                raise error from exc
            raise

    async def _abort_worker(self) -> None:
        async with self._start_lock:
            task = self._worker_task
            self._generation += 1
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._queue = None
            self._worker_task = None
            self._ready = None
            self._worker_error = None

    async def close(self) -> None:
        queue = self._queue
        task = self._worker_task
        if queue is not None and task is not None and not task.done():
            await queue.put(None)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except TimeoutError:
                await self._abort_worker()
        elif task is not None:
            await task
        self._queue = None
        self._worker_task = None
        self._ready = None
        self._worker_error = None
