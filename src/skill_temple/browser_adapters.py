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
from typing import Any, Protocol

from .browser_models import EventPredicate, FlowStep, Locator, RequestMatcher, WaitCondition


class AdapterError(RuntimeError):
    """Raised when a private browser adapter cannot complete an operation."""


class DeadlineLike(Protocol):
    def remaining_seconds(self) -> float: ...

    def ensure_remaining(self, operation: str) -> None: ...


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


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
class StreamWaitResult:
    condition_met: bool
    capture_id: int
    capture_version: int
    matched_request_ids: list[str]
    terminal_status: str | None = None
    matched_event: dict[str, Any] | None = None
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
            await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.communicate()

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
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=deadline.remaining_seconds()
            )
        except TimeoutError as exc:
            await self._terminate_tree(process)
            raise AdapterError(f"Command timed out: {argv[0]} {argv[-1]}") from exc
        result = CommandResult(
            argv=argv,
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
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
        self, session_ref: str, experiment_dir: Path, deadline: DeadlineLike
    ) -> list[str]: ...

    async def capture_screenshot(
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


class PlaywrightCliAdapter:
    """Fixed-argv wrapper around the existing playwright-cli."""

    def __init__(
        self,
        *,
        executable: str = "playwright-cli",
        runner: CommandRunner | None = None,
        cwd: Path | None = None,
    ) -> None:
        self.executable = executable
        self.runner = runner or SubprocessCommandRunner()
        self.cwd = cwd
        self._trace_files_before: dict[str, set[Path]] = {}
        self._selected_page_index: dict[str, int] = {}

    def _argv(self, session_ref: str, *parts: str, raw: bool = False) -> list[str]:
        argv = [self.executable, f"-s={session_ref}"]
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
        await self._run(
            session_ref,
            "attach",
            f"--cdp={browser_endpoint}",
            deadline=deadline,
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
        target = self.render_locator(step.locator) if step.locator else None
        if step.action == "navigate":
            result = await self._run(session_ref, "goto", step.value or "", deadline=deadline)
        elif step.action == "reload":
            result = await self._run(session_ref, "reload", deadline=deadline)
        elif step.action in {"click", "hover", "check", "uncheck"}:
            result = await self._run(session_ref, step.action, target or "", deadline=deadline)
        elif step.action in {"fill", "select"}:
            result = await self._run(
                session_ref, step.action, target or "", step.value or "", deadline=deadline
            )
        elif step.action in {"type", "press"}:
            result = await self._run(session_ref, step.action, step.value or "", deadline=deadline)
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
            elif condition.type == "network_idle":
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
        self, session_ref: str, experiment_dir: Path, deadline: DeadlineLike
    ) -> list[str]:
        result = await self._run(session_ref, "tracing-stop", deadline=deadline)
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

    async def close_session(self, session_ref: str, deadline: DeadlineLike) -> None:
        await self._run(session_ref, "detach", deadline=deadline, allow_failure=True)
        self._selected_page_index.pop(session_ref, None)


class McpToolTransport(Protocol):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], deadline: DeadlineLike
    ) -> dict[str, Any]: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class _McpCall:
    name: str
    arguments: dict[str, Any]
    timeout_seconds: float
    future: asyncio.Future[dict[str, Any]]


class StdioMcpToolTransport:
    """Persistent MCP stdio client owned by one dedicated asyncio task."""

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

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._worker_task is None:
                loop = asyncio.get_running_loop()
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
                    try:
                        result = await session.call_tool(
                            call.name,
                            call.arguments,
                            read_timeout_seconds=timedelta(
                                seconds=max(0.1, call.timeout_seconds)
                            ),
                        )
                        parsed = self._normalize_result(call.name, result)
                    except BaseException as exc:
                        if not call.future.cancelled():
                            call.future.set_exception(exc)
                        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                            raise
                    else:
                        if not call.future.cancelled():
                            call.future.set_result(parsed)
        except BaseException as exc:
            self._worker_error = exc
            if not ready.done():
                ready.set_exception(exc)
            while not queue.empty():
                pending = queue.get_nowait()
                if pending is not None and not pending.future.cancelled():
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
        await queue.put(
            _McpCall(
                name=name,
                arguments=arguments,
                timeout_seconds=timeout_seconds,
                future=future,
            )
        )
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as exc:
            raise AdapterError(f"MCP tool timed out: {name}") from exc

    async def close(self) -> None:
        queue = self._queue
        task = self._worker_task
        if queue is not None and task is not None and not task.done():
            await queue.put(None)
            await task
        elif task is not None:
            await task
        self._queue = None
        self._worker_task = None
        self._ready = None
        self._worker_error = None


class JsReverseAdapter(Protocol):
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
    ) -> dict[str, Any]: ...

    async def list_network_requests(
        self,
        matcher: RequestMatcher,
        deadline: DeadlineLike,
    ) -> dict[str, Any]: ...

    async def wait_for_stream_condition(
        self,
        *,
        capture_id: int,
        request_matcher: RequestMatcher,
        condition: WaitCondition,
        since_version: int,
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
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "captureId": capture_id,
            "pageIdx": 0,
            "pageSize": 100,
            "afterEventIndex": after_event_index,
        }
        if request_id:
            arguments["requestId"] = request_id
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
        return await self._call(
            "get_stream_status",
            arguments,
            deadline,
        )

    async def list_network_requests(
        self,
        matcher: RequestMatcher,
        deadline: DeadlineLike,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"pageIdx": 0, "pageSize": 100}
        if matcher.url_contains:
            arguments["urlFilter"] = matcher.url_contains
        if matcher.method:
            arguments["methods"] = [matcher.method]
        if matcher.resource_types:
            arguments["resourceTypes"] = matcher.resource_types
        return await self._call("list_network_requests", arguments, deadline)

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

    async def wait_for_stream_condition(
        self,
        *,
        capture_id: int,
        request_matcher: RequestMatcher,
        condition: WaitCondition,
        since_version: int,
        deadline: DeadlineLike,
    ) -> StreamWaitResult:
        last_payload: dict[str, Any] = {}
        after_event_index = -1
        while deadline.remaining_seconds() > 0:
            predicate = (
                condition.predicate
                if condition.type == "event_predicate"
                and condition.predicate
                and condition.predicate.type
                in {"exact_data", "event_name", "json_path_equals"}
                else None
            )
            payload = await self.get_stream_status(
                capture_id,
                deadline,
                request_id=request_matcher.request_id,
                event_predicate=predicate,
                after_event_index=after_event_index,
            )
            last_payload = payload
            capture = payload.get("capture") if isinstance(payload.get("capture"), dict) else {}
            version = int(capture.get("version", 0) or 0)
            requests = [
                item
                for item in payload.get("requests", [])
                if isinstance(item, dict) and self._request_matches(item, request_matcher)
            ]
            request_ids = [str(item.get("cdpRequestId", "")) for item in requests]
            terminal = next(
                (
                    str(item.get("status"))
                    for item in requests
                    if item.get("status") in {"finished", "canceled", "failed", "stopped"}
                ),
                None,
            )
            met = False
            matched_event: dict[str, Any] | None = None
            if condition.type == "first_event":
                met = any(int(item.get("rawEventCount", 0)) > 0 for item in requests)
                for item in requests:
                    recent = item.get("recentRawEvents")
                    if isinstance(recent, list) and recent:
                        matched_event = recent[-1] if isinstance(recent[-1], dict) else None
                        break
            elif condition.type == "default_done_marker":
                met = any(bool(item.get("defaultDoneMarkerObserved")) for item in requests)
            elif condition.type == "network_finished":
                met = any(item.get("status") == "finished" for item in requests)
            elif condition.type == "network_canceled":
                met = any(item.get("status") == "canceled" for item in requests)
            elif condition.type == "failed":
                met = any(item.get("status") == "failed" for item in requests)
            elif condition.type in {"request_observed", "response_observed"}:
                met = bool(requests) and (
                    condition.type == "request_observed"
                    or any(bool(item.get("responseObserved")) for item in requests)
                )
            elif condition.type == "event_predicate" and condition.predicate:
                if condition.predicate.type == "network_terminal":
                    desired = condition.predicate.value
                    met = bool(terminal) and (
                        desired is None or str(desired) == terminal
                    )
                else:
                    candidate = payload.get("eventMatch")
                    matched_event = candidate if isinstance(candidate, dict) else None
                    met = bool(matched_event and matched_event.get("matched"))
                    if matched_event and isinstance(
                        matched_event.get("matchedEventIndex"), int
                    ):
                        after_event_index = int(matched_event["matchedEventIndex"])
            if met and version > since_version:
                return StreamWaitResult(
                    condition_met=True,
                    capture_id=capture_id,
                    capture_version=version,
                    matched_request_ids=request_ids,
                    terminal_status=terminal,
                    matched_event=matched_event,
                    status_payload=payload,
                )
            await asyncio.sleep(min(0.2, max(0.01, deadline.remaining_seconds())))
        return StreamWaitResult(
            condition_met=False,
            capture_id=capture_id,
            capture_version=int((last_payload.get("capture") or {}).get("version", 0) or 0),
            matched_request_ids=[],
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
