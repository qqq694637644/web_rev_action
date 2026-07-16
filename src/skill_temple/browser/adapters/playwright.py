"""Playwright CLI transport implementation."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from ...browser_models import FlowStep, Locator, WaitCondition
from .command import SubprocessCommandRunner
from .contracts import (
    AdapterError,
    CommandResult,
    CommandRunner,
    DeadlineLike,
    PageState,
)

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
        except json.JSONDecodeError as exc:
            raise AdapterError(
                "playwright-cli --raw current-page output was not valid JSON",
                dispatch_started=True,
                outcome_unknown=False,
            ) from exc
        if not isinstance(parsed, dict):
            raise AdapterError(
                "playwright-cli --raw current-page output was not a JSON object",
                dispatch_started=True,
                outcome_unknown=False,
            )
        url = parsed.get("url")
        title = parsed.get("title")
        if not isinstance(url, str) or not isinstance(title, str):
            raise AdapterError(
                "playwright-cli current-page JSON must contain string url and title fields",
                dispatch_started=True,
                outcome_unknown=False,
            )
        return PageState(
            url=url,
            title=title,
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
                )
                visible = bool(result.stdout.strip())
                if visible == (condition.type == "selector_visible"):
                    return {"condition_met": True, "type": condition.type}
            elif condition.type == "request_log_stable":
                result = await self._run(
                    session_ref,
                    "requests",
                    deadline=deadline,
                    raw=True,
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
        await self._run(session_ref, "detach", deadline=deadline)
        self._selected_page_index.pop(session_ref, None)
