"""Private adapters for playwright-cli and js-reverse-mcp."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from .browser_models import (
    EventPredicate,
    ExactDataPredicate,
    FlowStep,
    Locator,
    NetworkExportPart,
    RequestMatcher,
    WaitCondition,
)


class AdapterError(RuntimeError):
    """Raised when a private browser adapter cannot complete an operation."""


class McpToolCallError(AdapterError):
    def __init__(
        self,
        message: str,
        *,
        outcome_unknown: bool,
        transport_generation: int,
    ) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown
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


class SubprocessCommandRunner:
    def __init__(self, *, max_output_bytes: int = 1_000_000) -> None:
        self.max_output_bytes = max_output_bytes

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader | None,
        state: dict[str, Any],
        key: str,
    ) -> None:
        if stream is None:
            return
        parts: list[bytes] = state[key]
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return
            remaining = int(state["remaining"])
            if remaining > 0:
                kept = chunk[:remaining]
                parts.append(kept)
                state["remaining"] = remaining - len(kept)
            if len(chunk) > remaining:
                state["truncated"] = True

    async def _collect_output(
        self,
        process: asyncio.subprocess.Process,
        timeout: float,
    ) -> tuple[bytes, bytes, bool]:
        state: dict[str, Any] = {
            "remaining": self.max_output_bytes,
            "truncated": False,
            "stdout": [],
            "stderr": [],
        }
        readers = [
            asyncio.create_task(
                self._read_bounded(process.stdout, state, "stdout")
            ),
            asyncio.create_task(
                self._read_bounded(process.stderr, state, "stderr")
            ),
        ]
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            await self._terminate_tree(process)
            raise
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._terminate_tree(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise
        finally:
            await asyncio.gather(*readers, return_exceptions=True)
        return (
            b"".join(state["stdout"]),
            b"".join(state["stderr"]),
            bool(state["truncated"]),
        )

    async def _terminate_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.communicate()
        else:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def run(
        self,
        argv: list[str],
        *,
        deadline: DeadlineLike,
        cwd: Path | None = None,
        allow_failure: bool = False,
    ) -> CommandResult:
        deadline.ensure_remaining("subprocess")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        try:
            stdout, stderr, truncated = await self._collect_output(
                process,
                deadline.remaining_seconds(),
            )
        except TimeoutError as exc:
            raise AdapterError(f"Command timed out: {argv[0]} {argv[-1]}") from exc
        result = CommandResult(
            argv=argv,
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            truncated=truncated,
        )
        if result.returncode != 0 and not allow_failure:
            message = (result.stderr or result.stdout).strip()[-4000:]
            raise AdapterError(f"Command failed ({result.returncode}): {message}")
        return result


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


_PAGE_URL_RE = re.compile(r"^- Page URL:\s*(.+)$", re.MULTILINE)
_PAGE_TITLE_RE = re.compile(r"^- Page Title:\s*(.+)$", re.MULTILINE)
_SNAPSHOT_RE = re.compile(r"\[Snapshot\]\(([^)]+)\)")


def build_playwright_attach_args(endpoint: str, session_ref: str) -> list[str]:
    return ["attach", "--cdp", endpoint, "--session", session_ref]


class PlaywrightCliAdapter:
    """Fixed-argv wrapper around the existing playwright-cli."""

    def __init__(
        self,
        *,
        executable: str = "playwright-cli",
        command_prefix: list[str] | None = None,
        runner: CommandRunner | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.executable = executable
        self.command_prefix = command_prefix or [executable]
        self.runner = runner or SubprocessCommandRunner()
        self.cwd = cwd
        self._trace_files_before: dict[str, set[Path]] = {}
        self._selected_page_index: dict[str, int] = {}

    def _argv(self, session_ref: str, *parts: str, raw: bool = False) -> list[str]:
        argv = [*self.command_prefix, f"-s={session_ref}"]
        if raw:
            argv.append("--raw")
        argv.extend(parts)
        return argv

    async def _run(
        self,
        session_ref: str,
        *parts: str,
        deadline: DeadlineLike,
        raw: bool = False,
        allow_failure: bool = False,
    ) -> CommandResult:
        return await self.runner.run(
            self._argv(session_ref, *parts, raw=raw),
            deadline=deadline,
            cwd=self.cwd,
            allow_failure=allow_failure,
        )

    async def open_session(
        self,
        session_ref: str,
        browser_endpoint: str,
        start_url: str | None,
        deadline: DeadlineLike,
    ) -> PageState:
        await self.runner.run(
            [
                *self.command_prefix,
                *build_playwright_attach_args(browser_endpoint, session_ref),
            ],
            deadline=deadline,
            cwd=self.cwd,
        )
        self._selected_page_index[session_ref] = 0
        if start_url:
            await self._run(session_ref, "goto", start_url, deadline=deadline)
        return await self.current_page(session_ref, deadline)

    async def current_page(self, session_ref: str, deadline: DeadlineLike) -> PageState:
        expression = "JSON.stringify({url:location.href,title:document.title})"
        result = await self._run(
            session_ref,
            "eval",
            expression,
            deadline=deadline,
            raw=True,
        )
        raw = result.stdout.strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, str):
                parsed = json.loads(parsed)
            if isinstance(parsed, dict):
                return PageState(
                    url=str(parsed.get("url", "")),
                    title=str(parsed.get("title", "")),
                    page_index=self._selected_page_index.get(session_ref, 0),
                )
        except json.JSONDecodeError:
            pass
        url_match = _PAGE_URL_RE.search(result.stdout)
        title_match = _PAGE_TITLE_RE.search(result.stdout)
        if not url_match:
            raise AdapterError("playwright-cli did not return the current page URL")
        return PageState(
            url=url_match.group(1).strip(),
            title=title_match.group(1).strip() if title_match else "",
            page_index=self._selected_page_index.get(session_ref, 0),
        )

    async def select_page(
        self,
        session_ref: str,
        page_index: int,
        deadline: DeadlineLike,
    ) -> PageState:
        await self._run(
            session_ref,
            "tab-select",
            str(page_index),
            deadline=deadline,
        )
        self._selected_page_index[session_ref] = page_index
        return await self.current_page(session_ref, deadline)

    @staticmethod
    def _quote_locator(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    def render_locator(self, locator: Locator) -> str:
        if locator.ref:
            return locator.ref
        if locator.css:
            return locator.css
        if locator.role:
            return (
                f"getByRole({self._quote_locator(locator.role)}, "
                f"{{ name: {self._quote_locator(locator.name or '')} }})"
            )
        if locator.label:
            return f"getByLabel({self._quote_locator(locator.label)})"
        if locator.placeholder:
            return f"getByPlaceholder({self._quote_locator(locator.placeholder)})"
        if locator.test_id:
            return f"getByTestId({self._quote_locator(locator.test_id)})"
        if locator.text:
            return f"getByText({self._quote_locator(locator.text)})"
        raise AdapterError("Unsupported locator")

    async def execute_step(
        self,
        session_ref: str,
        step: FlowStep,
        experiment_dir: Path,
        deadline: DeadlineLike,
    ) -> dict[str, Any]:
        locator = getattr(step, "locator", None)
        target = self.render_locator(locator) if locator else None
        if step.action == "navigate":
            result = await self._run(session_ref, "goto", step.value, deadline=deadline)
        elif step.action == "reload":
            result = await self._run(session_ref, "reload", deadline=deadline)
        elif step.action in {"click", "hover", "check", "uncheck"}:
            result = await self._run(session_ref, step.action, target or "", deadline=deadline)
        elif step.action in {"fill", "select"}:
            result = await self._run(
                session_ref, step.action, target or "", step.value, deadline=deadline
            )
        elif step.action in {"type", "press"}:
            result = await self._run(session_ref, step.action, step.value, deadline=deadline)
        elif step.action == "upload":
            if target:
                await self._run(session_ref, "click", target, deadline=deadline)
            result = await self._run(session_ref, "upload", *step.values, deadline=deadline)
        elif step.action == "snapshot":
            filename = experiment_dir / "playwright" / f"{step.step_id}.yaml"
            filename.parent.mkdir(parents=True, exist_ok=True)
            result = await self._run(
                session_ref,
                "snapshot",
                f"--filename={filename}",
                deadline=deadline,
            )
        else:
            raise AdapterError(f"Step {step.action} must be handled by the orchestrator")
        snapshot_match = _SNAPSHOT_RE.search(result.stdout)
        return {
            "stdout": result.stdout[-8000:],
            "snapshot_ref": snapshot_match.group(1) if snapshot_match else None,
        }

    async def wait_for_page_condition(
        self,
        session_ref: str,
        condition: WaitCondition,
        deadline: DeadlineLike,
    ) -> dict[str, Any]:
        if condition.type == "timeout":
            await asyncio.sleep(min(condition.timeout_ms / 1000, deadline.remaining_seconds()))
            return {"condition_met": True, "type": condition.type}
        poll_deadline = min(condition.timeout_ms / 1000, deadline.remaining_seconds())
        loop = asyncio.get_running_loop()
        end = loop.time() + poll_deadline
        network_signature: str | None = None
        network_stable_since = loop.time()
        while loop.time() < end:
            if condition.type == "page_url":
                page = await self.current_page(session_ref, deadline)
                if condition.value and condition.value in page.url:
                    return {"condition_met": True, "type": condition.type, "url": page.url}
            elif condition.type in {"selector_visible", "selector_hidden"}:
                target = self.render_locator(condition.locator) if condition.locator else ""
                result = await self._run(
                    session_ref,
                    "snapshot",
                    target,
                    deadline=deadline,
                    raw=True,
                    allow_failure=True,
                )
                visible = result.returncode == 0 and bool(result.stdout.strip())
                if visible == (condition.type == "selector_visible"):
                    return {"condition_met": True, "type": condition.type}
            elif condition.type == "request_log_stable":
                result = await self._run(
                    session_ref,
                    "requests",
                    deadline=deadline,
                    raw=True,
                    allow_failure=True,
                )
                signature = result.stdout.strip()
                if signature != network_signature:
                    network_signature = signature
                    network_stable_since = loop.time()
                elif loop.time() - network_stable_since >= 0.5:
                    return {"condition_met": True, "type": condition.type}
            else:
                raise AdapterError(f"Unsupported page wait condition: {condition.type}")
            await asyncio.sleep(min(0.2, max(0.01, end - loop.time())))
        raise AdapterError(f"Page wait condition timed out: {condition.type}")

    async def start_trace(self, session_ref: str, deadline: DeadlineLike) -> None:
        self._trace_files_before[session_ref] = self._trace_files()
        await self._run(session_ref, "tracing-start", deadline=deadline)

    async def stop_trace(
        self,
        session_ref: str,
        experiment_dir: Path,
        deadline: DeadlineLike,
        *,
        collect_files: bool = True,
    ) -> list[str]:
        result = await self._run(session_ref, "tracing-stop", deadline=deadline)
        if not collect_files:
            self._trace_files_before.pop(session_ref, None)
            return []
        base = self.cwd or Path.cwd()
        candidates: set[Path] = set()
        for raw in re.findall(
            r"(?:[A-Za-z]:)?[^\s\[\]()]+\.(?:trace|network|zip)",
            result.stdout,
        ):
            path = Path(raw)
            candidates.add(path if path.is_absolute() else (base / path))
        before = self._trace_files_before.pop(session_ref, set())
        candidates.update(self._trace_files() - before)
        target_dir = experiment_dir / "playwright" / "traces"
        target_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        for source in sorted(candidates):
            if not source.is_file():
                continue
            destination = target_dir / source.name
            if destination.exists():
                destination = target_dir / f"{source.stem}-{len(saved) + 1}{source.suffix}"
            shutil.copy2(source, destination)
            saved.append(destination.as_posix())
            if len(saved) >= 50:
                break
        return saved

    def _trace_files(self) -> set[Path]:
        base = self.cwd or Path.cwd()
        output_root = base / ".playwright-cli"
        if not output_root.is_dir():
            return set()
        extensions = {".trace", ".network", ".zip"}
        return {
            path.resolve()
            for path in output_root.rglob("*")
            if path.is_file() and path.suffix.lower() in extensions
        }

    async def capture_screenshot(
        self,
        session_ref: str,
        experiment_dir: Path,
        name: str,
        deadline: DeadlineLike,
    ) -> str:
        filename = experiment_dir / "playwright" / "screenshots" / f"{name}.png"
        filename.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            session_ref,
            "screenshot",
            f"--filename={filename}",
            deadline=deadline,
        )
        return filename.as_posix()

    async def capture_snapshot(
        self,
        session_ref: str,
        experiment_dir: Path,
        name: str,
        deadline: DeadlineLike,
    ) -> str:
        filename = experiment_dir / "playwright" / "snapshots" / f"{name}.yaml"
        filename.parent.mkdir(parents=True, exist_ok=True)
        await self._run(
            session_ref,
            "snapshot",
            f"--filename={filename}",
            deadline=deadline,
        )
        return filename.as_posix()

    async def close_session(self, session_ref: str, deadline: DeadlineLike) -> None:
        await self._run(session_ref, "detach", deadline=deadline, allow_failure=True)
        self._selected_page_index.pop(session_ref, None)


class McpToolTransport(Protocol):
    @property
    def generation(self) -> int: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any], deadline: DeadlineLike
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


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
            return any(
                StdioMcpToolTransport._is_transport_failure(item)
                for item in exc.exceptions
            )
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
            stale_worker = (
                self._worker_task is not None
                and (
                    self._worker_task.done()
                    or self._ready is None
                    or self._ready.cancelled()
                )
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
                session = await stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
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
                            call.absolute_deadline
                            - asyncio.get_running_loop().time(),
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
                                f"MCP transport failed during {call.name}: {exc}"
                            )
                        elif call.name in self.SIDE_EFFECTING_TOOLS:
                            delivered = McpToolCallError(
                                f"MCP tool failed after dispatch: {call.name}: {exc}",
                                outcome_unknown=call.sent,
                                transport_generation=generation,
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
                raise AdapterError(
                    str(error.get("message") or f"MCP tool failed: {name}")
                )
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
                outcome_unknown=call.sent,
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
                raise AdapterError(
                    f"MCP transport failed and was restarted: {name}: {exc}"
                ) from exc
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

    async def get_request_initiator(
        self, reqid: int, deadline: DeadlineLike
    ) -> dict[str, Any]: ...

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
        candidates = indexed or exact or [
            item
            for item in pages
            if page.url
            and page.url.rstrip("/") == str(item.get("url", "")).rstrip("/")
        ]
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
                ["Multiple matching pages; selected the first."]
                if len(candidates) > 1
                else []
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
                requests.extend(
                    item for item in page_requests if isinstance(item, dict)
                )
            pagination = (
                page.get("pagination")
                if isinstance(page.get("pagination"), dict)
                else {}
            )
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
                requests.extend(
                    item for item in page_requests if isinstance(item, dict)
                )
            pagination = (
                page.get("pagination")
                if isinstance(page.get("pagination"), dict)
                else {}
            )
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

    async def get_request_initiator(
        self, reqid: int, deadline: DeadlineLike
    ) -> dict[str, Any]:
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
            pagination = (
                page.get("pagination")
                if isinstance(page.get("pagination"), dict)
                else {}
            )
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
            pagination = (
                page.get("pagination")
                if isinstance(page.get("pagination"), dict)
                else {}
            )
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
        function = r"""async ({localFile}) => {
          const spec = JSON.parse(localFile.text);
          const headers = new Headers();
          for (const entry of spec.headers || []) {
            headers.append(String(entry.name), String(entry.value));
          }
          let body;
          if (spec.body && spec.body.encoding === 'utf8') {
            body = spec.body.text;
          } else if (spec.body && spec.body.encoding === 'base64') {
            const binary = atob(spec.body.base64 || '');
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
            body = bytes;
          }
          const response = await fetch(spec.url, {
            method: spec.method,
            headers,
            body: ['GET', 'HEAD'].includes(String(spec.method).toUpperCase()) ? undefined : body,
            credentials: 'include',
            redirect: 'follow',
          });
          const responseControl = spec.responseControl || {};
          const maxResponseBytes = Math.max(
            8192,
            Number(responseControl.maxResponseBytes || 8 * 1024 * 1024),
          );
          const idleTimeoutMs = Math.max(
            1000,
            Number(responseControl.idleTimeoutMs || 15000),
          );
          const doneMarker = responseControl.doneMarker == null
            ? null
            : String(responseControl.doneMarker);
          const doneEventName = responseControl.doneEventName == null
            ? null
            : String(responseControl.doneEventName);
          const reader = response.body ? response.body.getReader() : null;
          const decoder = new TextDecoder('utf-8', {fatal: false});
          const previewChunks = [];
          let previewByteLength = 0;
          let bodyByteLength = 0;
          let doneMarkerObserved = false;
          let doneEventNameObserved = null;
          let truncated = false;
          let terminationReason = reader ? 'network_close' : 'no_response_body';
          let sseBuffer = '';
          const consumeSseEvents = (text) => {
            sseBuffer += text;
            while (true) {
              const match = /\r?\n\r?\n/.exec(sseBuffer);
              if (!match) return false;
              const block = sseBuffer.slice(0, match.index);
              sseBuffer = sseBuffer.slice(match.index + match[0].length);
              let eventName = 'message';
              const dataLines = [];
              for (const line of block.split(/\r?\n/)) {
                if (!line || line.startsWith(':')) continue;
                const colon = line.indexOf(':');
                const field = colon < 0 ? line : line.slice(0, colon);
                let fieldValue = colon < 0 ? '' : line.slice(colon + 1);
                if (fieldValue.startsWith(' ')) fieldValue = fieldValue.slice(1);
                if (field === 'event') eventName = fieldValue;
                if (field === 'data') dataLines.push(fieldValue);
              }
              const data = dataLines.join('\n');
              if (
                doneMarker &&
                data === doneMarker &&
                (!doneEventName || eventName === doneEventName)
              ) {
                doneEventNameObserved = eventName;
                return true;
              }
            }
          };
          const readWithIdleTimeout = async () => {
            let timer;
            try {
              return await Promise.race([
                reader.read(),
                new Promise((_, reject) => {
                  timer = setTimeout(
                    () => reject(new Error('__REPLAY_IDLE_TIMEOUT__')),
                    idleTimeoutMs,
                  );
                }),
              ]);
            } finally {
              if (timer) clearTimeout(timer);
            }
          };
          if (reader) {
            while (true) {
              let readResult;
              try {
                readResult = await readWithIdleTimeout();
              } catch (error) {
                if (String(error && error.message) !== '__REPLAY_IDLE_TIMEOUT__') throw error;
                terminationReason = 'idle_timeout';
                await reader.cancel('idle_timeout').catch(() => {});
                break;
              }
              if (readResult.done) {
                if (doneMarker) consumeSseEvents(decoder.decode());
                terminationReason = 'network_close';
                break;
              }
              const chunk = readResult.value || new Uint8Array();
              const remaining = maxResponseBytes - bodyByteLength;
              if (remaining <= 0) {
                truncated = true;
                terminationReason = 'max_response_bytes';
                await reader.cancel('max_response_bytes').catch(() => {});
                break;
              }
              const accepted = chunk.subarray(0, Math.min(chunk.byteLength, remaining));
              bodyByteLength += accepted.byteLength;
              if (previewByteLength < 8192) {
                const previewPart = accepted.subarray(
                  0,
                  Math.min(accepted.byteLength, 8192 - previewByteLength),
                );
                previewChunks.push(previewPart);
                previewByteLength += previewPart.byteLength;
              }
              if (doneMarker) {
                if (consumeSseEvents(decoder.decode(accepted, {stream: true}))) {
                  doneMarkerObserved = true;
                  terminationReason = 'done_marker';
                  await reader.cancel('done_marker').catch(() => {});
                  break;
                }
              }
              if (accepted.byteLength < chunk.byteLength || bodyByteLength >= maxResponseBytes) {
                truncated = true;
                terminationReason = 'max_response_bytes';
                await reader.cancel('max_response_bytes').catch(() => {});
                break;
              }
            }
          }
          const previewBytes = new Uint8Array(previewByteLength);
          let previewOffset = 0;
          for (const chunk of previewChunks) {
            previewBytes.set(chunk, previewOffset);
            previewOffset += chunk.byteLength;
          }
          let preview = '';
          try { preview = new TextDecoder('utf-8', {fatal: false}).decode(previewBytes); }
          catch { preview = ''; }
          return {
            status: response.status,
            statusText: response.statusText,
            url: response.url,
            redirected: response.redirected,
            ok: response.ok,
            headers: Array.from(response.headers.entries()),
            bodyByteLength,
            bodyPreview: preview,
            doneMarkerObserved,
            doneEventNameObserved,
            terminationReason,
            truncated,
            maxResponseBytes,
            idleTimeoutMs,
          };
        }"""
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
        return str(
            request.get("cdpRequestId")
            or request.get("persistentRequestId")
            or ""
        )

    @staticmethod
    def _request_checkpoint(request: dict[str, Any]) -> StreamRequestCheckpoint:
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
                request_id: item
                for item in requests
                if (request_id := self._request_id(item))
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
                    for source, prior_index in self._advanced_event_sources(
                        current, previous
                    ):
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
                    terminal_status = (
                        next(iter(statuses)) if len(statuses) == 1 else None
                    )
                else:
                    for request_id, request in request_by_id.items():
                        current = current_checkpoint.requests[request_id]
                        previous = checkpoint.requests.get(request_id)
                        for source, prior_index in self._advanced_event_sources(
                            current, previous
                        ):
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

    async def stop_stream_capture(
        self, capture_id: int, deadline: DeadlineLike
    ) -> dict[str, Any]:
        remaining_ms = max(100, min(34_000, int(deadline.remaining_seconds() * 1000)))
        return await self._call(
            "stop_stream_capture",
            {"captureId": capture_id, "finalizeTimeoutMs": remaining_ms},
            deadline,
        )

    async def close(self) -> None:
        await self.transport.close()
