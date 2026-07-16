from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import urlopen

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from toolchain_validation_server import SSE_EVENTS, start_server

from skill_temple.browser.adapters.playwright import build_playwright_attach_args

PLAYWRIGHT_PACKAGE = "@playwright/cli@0.1.17"
JS_REVERSE_COMMAND = "js-reverse-mcp"
SESSION_NAME = "stage0-toolchain-validation"


@dataclass
class ValidationResult:
    name: str
    passed: bool
    evidence: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def status(self) -> str:
        return "PASS" if self.passed else "FAIL"


@dataclass
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    def require_success(self) -> CommandResult:
        if self.returncode != 0:
            raise RuntimeError(
                f"Command failed with exit code {self.returncode}: {self.stderr or self.stdout}"
            )
        return self


class PlaywrightCli:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def run(self, args: Sequence[str], timeout: float = 30.0) -> CommandResult:
        command = build_npx_command(PLAYWRIGHT_PACKAGE, args)
        completed = subprocess.run(
            command,
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            args=tuple(command),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def start(self, args: Sequence[str]) -> subprocess.Popen[str]:
        command = build_npx_command(PLAYWRIGHT_PACKAGE, args)
        return subprocess.Popen(
            command,
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    def attach(self, endpoint: str) -> None:
        self.run(
            build_playwright_attach_args(endpoint, SESSION_NAME),
            timeout=45.0,
        ).require_success()

    def session(self, *args: str, timeout: float = 30.0) -> CommandResult:
        return self.run([f"-s={SESSION_NAME}", *args], timeout=timeout)

    def click_button(self, element_id: str, accessible_name: str) -> None:
        direct = self.session("click", f"#{element_id}", timeout=15.0)
        if direct.returncode == 0:
            return

        snapshot = self.session("snapshot", "--json", timeout=20.0).require_success()
        reference = find_snapshot_reference(snapshot.stdout, accessible_name)
        if reference is None:
            raise RuntimeError(
                f"Unable to resolve button {accessible_name!r}. Direct click error: {direct.stderr}"
            )
        self.session("click", reference, timeout=20.0).require_success()

    def start_button_click(
        self,
        element_id: str,
        accessible_name: str,
    ) -> subprocess.Popen[str]:
        direct = self.start([f"-s={SESSION_NAME}", "click", f"#{element_id}"])
        time.sleep(0.5)
        if direct.poll() is None or direct.returncode == 0:
            return direct

        _, stderr = direct.communicate(timeout=5.0)
        snapshot = self.session("snapshot", "--json", timeout=20.0).require_success()
        reference = find_snapshot_reference(snapshot.stdout, accessible_name)
        if reference is None:
            raise RuntimeError(
                f"Unable to resolve button {accessible_name!r}. Direct click error: {stderr}"
            )
        return self.start([f"-s={SESSION_NAME}", "click", reference])

    def wait_for_text(self, text: str, timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        last_output = ""
        while time.monotonic() < deadline:
            result = self.session("find", text, "--json", timeout=10.0)
            last_output = f"{result.stdout}\n{result.stderr}"
            if result.returncode == 0 and text in last_output:
                return
            time.sleep(0.25)
        raise TimeoutError(f"Page text {text!r} was not observed. Last output: {last_output}")


class McpToolClient:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self.session.call_tool(name, arguments or {})
        payload = model_to_dict(result)
        if payload.get("isError"):
            raise RuntimeError(f"MCP tool {name} returned an error: {payload}")

        structured = payload.get("structuredContent")
        if isinstance(structured, dict):
            tool_payload = structured
        else:
            tool_payload = parse_tool_text_content(payload)

        if tool_payload.get("ok") is False:
            raise RuntimeError(f"MCP tool {name} failed: {tool_payload}")
        return tool_payload


class Stage0Validation:
    def __init__(self, repo_root: Path, js_reverse_entry: Path | None = None) -> None:
        self.repo_root = repo_root
        self.js_reverse_entry = js_reverse_entry.resolve() if js_reverse_entry else None
        self.fixture_root = repo_root / "tests" / "fixtures" / "toolchain_validation"
        self.report_path = (
            repo_root / "data" / "analysis-workspace" / "reports" / "toolchain-validation.md"
        )
        self.results: list[ValidationResult] = []
        self.environment: dict[str, str] = {}
        self.playwright = PlaywrightCli(repo_root)

    async def run(self) -> int:
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.environment["python"] = sys.version.split()[0]
        self.environment["playwright-cli"] = self._package_version(PLAYWRIGHT_PACKAGE)
        self.environment["js-reverse-mcp"] = self._js_reverse_version()

        server, server_thread = start_server(self.fixture_root)
        fixture_port = int(server.server_address[1])
        fixture_url = f"http://127.0.0.1:{fixture_port}/"

        cdp_port = find_free_port()
        endpoint = f"http://127.0.0.1:{cdp_port}"
        chrome_profile = Path(tempfile.mkdtemp(prefix="stage0-chrome-"))
        export_root = Path(
            tempfile.mkdtemp(prefix="stage0-evidence-", dir=self.report_path.parent.parent)
        )
        chrome_process: subprocess.Popen[bytes] | None = None

        try:
            chrome_process = start_chrome(cdp_port, chrome_profile)
            wait_for_http(f"{endpoint}/json/version", timeout=20.0)
            self.playwright.attach(endpoint)
            self.playwright.session("goto", fixture_url, timeout=30.0).require_success()

            server_parameters = build_mcp_server_parameters(
                endpoint,
                self.repo_root,
                export_root,
                self.js_reverse_entry,
            )
            async with stdio_client(server_parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    tool_names = {tool.name for tool in tools.tools}
                    required_tools = {
                        "break_on_xhr",
                        "clear_network_requests",
                        "get_paused_info",
                        "get_request_initiator",
                        "get_script_source",
                        "list_breakpoints",
                        "list_network_requests",
                        "pause_or_resume",
                        "remove_breakpoint",
                        "search_in_sources",
                        "select_page",
                        "start_stream_capture",
                        "get_stream_status",
                        "stop_stream_capture",
                    }
                    missing = sorted(required_tools - tool_names)
                    if missing:
                        raise RuntimeError(f"js-reverse-mcp is missing required tools: {missing}")

                    client = McpToolClient(session)
                    await self._validate_page_alignment(client, fixture_url)
                    request_ids = await self._validate_network_and_bodies(
                        client,
                        export_root,
                    )
                    await self._validate_initiator(client, request_ids["echo"])
                    await self._validate_sources(client, fixture_url)
                    await self._validate_xhr_breakpoint(client)

        except Exception as exc:
            diagnostic = "".join(traceback.format_exception(exc))
            print(diagnostic, file=sys.stderr)
            self.results.append(
                ValidationResult(
                    name="validation harness completion",
                    passed=False,
                    error=diagnostic,
                )
            )
        finally:
            try:
                self.playwright.session("detach", timeout=15.0)
            except Exception:
                pass
            if chrome_process is not None:
                stop_process(chrome_process)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5.0)
            shutil.rmtree(chrome_profile, ignore_errors=True)
            shutil.rmtree(export_root, ignore_errors=True)
            shutil.rmtree(self.repo_root / ".playwright", ignore_errors=True)
            shutil.rmtree(self.repo_root / ".playwright-cli", ignore_errors=True)

        self._validate_local_evidence_write()
        self._ensure_required_result_rows()
        self._write_report()
        return 0 if all(result.passed for result in self.results) else 1

    def _package_version(self, package: str) -> str:
        result = run_command(build_npx_command(package, ["--version"]), self.repo_root, 45.0)
        result.require_success()
        return result.stdout.strip()

    def _js_reverse_version(self) -> str:
        if self.js_reverse_entry is None:
            command = shutil.which(JS_REVERSE_COMMAND)
            if command is None:
                raise RuntimeError(
                    "js-reverse-mcp was not found on PATH; clone "
                    "qqq694637644/js-reverse-mcp, run npm install, npm run build, "
                    "and npm link"
                )
            result = run_command([command, "--version"], self.repo_root, 45.0)
            result.require_success()
            return result.stdout.strip()
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node is required for a local js-reverse-mcp entrypoint")
        result = run_command(
            [node, str(self.js_reverse_entry), "--version"],
            self.repo_root,
            45.0,
        )
        result.require_success()
        return f"{result.stdout.strip()} (local entry)"

    async def _validate_page_alignment(
        self,
        client: McpToolClient,
        fixture_url: str,
    ) -> None:
        try:
            playwright_tabs = self.playwright.session("tab-list", "--json").require_success()
            playwright_meta = extract_page_metadata(playwright_tabs.stdout, fixture_url)

            page_listing = await client.call("select_page", {})
            page_index = extract_page_index(page_listing, fixture_url)
            if page_index is not None:
                page_listing = await client.call("select_page", {"pageIdx": page_index})
            reverse_meta = extract_page_metadata(json.dumps(page_listing), fixture_url)

            normalized_fixture = normalize_url(fixture_url)
            urls_match = (
                normalize_url(playwright_meta["url"]) == normalized_fixture
                and normalize_url(reverse_meta["url"]) == normalized_fixture
            )
            titles_match = playwright_meta["title"] == reverse_meta["title"]
            if not urls_match or not titles_match:
                raise AssertionError(
                    f"Page mismatch: playwright={playwright_meta}, js-reverse={reverse_meta}"
                )
            self.results.append(
                ValidationResult(
                    name="Playwright current page aligns with js-reverse target",
                    passed=True,
                    evidence=[
                        "Both tools selected the same loopback fixture page.",
                        f"Title matched: {playwright_meta['title']}",
                    ],
                )
            )
        except Exception as exc:
            self.results.append(
                ValidationResult(
                    name="Playwright current page aligns with js-reverse target",
                    passed=False,
                    error=str(exc),
                )
            )
            raise

    async def _validate_network_and_bodies(
        self,
        client: McpToolClient,
        export_root: Path,
    ) -> dict[str, int]:
        await client.call("clear_network_requests", {"confirm": True})
        await client.call("list_network_requests", {"pageSize": 20})

        stream_start = await client.call(
            "start_stream_capture",
            {
                "artifactNamespace": "stage0-toolchain",
                "urlFilter": "/api/sse",
                "methods": ["GET"],
                "resourceTypes": ["eventsource"],
                "mimeTypes": ["text/event-stream"],
                "includeInFlight": False,
            },
        )
        capture_id = extract_integer(stream_start, "captureId")

        self.playwright.click_button("run-capture", "Run capture")
        self.playwright.wait_for_text("capture-complete", timeout=20.0)

        stream_status: dict[str, Any] | None = None
        event_match: dict[str, Any] | None = None
        for _ in range(100):
            stream_status = await client.call(
                "get_stream_status",
                {
                    "captureId": capture_id,
                    "eventPredicate": {
                        "type": "exact_data",
                        "value": "fixture-complete",
                    },
                    "afterEventIndex": -1,
                    "pageIdx": 0,
                    "pageSize": 100,
                },
            )
            event_match = find_mapping(stream_status, "matched", True)
            if event_match is not None:
                break
            await asyncio.sleep(0.05)
        if stream_status is None or event_match is None:
            raise TimeoutError(
                "Raw stream collector did not match the fixture-complete event."
            )

        stream_stop = await client.call(
            "stop_stream_capture",
            {"captureId": capture_id, "finalizeTimeoutMs": 10_000},
        )

        echo_listing = await client.call(
            "list_network_requests",
            {
                "methods": ["POST"],
                "urlFilter": "/api/echo",
                "pageSize": 20,
            },
        )
        sse_listing = await client.call(
            "list_network_requests",
            {
                "resourceTypes": ["eventsource"],
                "urlFilter": "/api/sse",
                "pageSize": 20,
            },
        )
        echo_request_id = extract_request_id(echo_listing, "/api/echo")
        sse_request_id = extract_request_id(sse_listing, "/api/sse")

        self.results.append(
            ValidationResult(
                name="Network request capture",
                passed=True,
                evidence=[
                    "Captured POST /api/echo.",
                    "Captured EventSource /api/sse.",
                ],
            )
        )

        request_body_path = export_root / "echo-request.json"
        response_body_path = export_root / "echo-response.json"

        await client.call(
            "list_network_requests",
            {
                "reqid": echo_request_id,
                "outputFile": str(request_body_path),
                "outputPart": "requestBody",
            },
        )
        await client.call(
            "list_network_requests",
            {
                "reqid": echo_request_id,
                "outputFile": str(response_body_path),
                "outputPart": "responseBody",
            },
        )
        request_body = request_body_path.read_bytes()
        response_body = response_body_path.read_bytes()

        request_json = json.loads(request_body.decode("utf-8"))
        response_json = json.loads(response_body.decode("utf-8"))
        if request_json.get("marker") != "synthetic-request":
            raise AssertionError(f"Unexpected request body: {request_json}")
        if response_json.get("marker") != "stage0-response":
            raise AssertionError(f"Unexpected response body: {response_json}")

        self.results.append(
            ValidationResult(
                name="Request and response body export",
                passed=True,
                evidence=[
                    f"Request body: {len(request_body)} bytes, sha256={sha256_bytes(request_body)}",
                    (
                        f"Response body: {len(response_body)} bytes, "
                        f"sha256={sha256_bytes(response_body)}"
                    ),
                ],
            )
        )

        raw_descriptor = extract_artifact(stream_status, "raw_bytes")
        events_descriptor = extract_artifact(stream_status, "events")
        capture_descriptor = extract_artifact(stream_stop, "capture_metadata")
        raw_path = resolve_artifact(export_root, raw_descriptor)
        events_path = resolve_artifact(export_root, events_descriptor)
        capture_path = resolve_artifact(export_root, capture_descriptor)
        raw_bytes = raw_path.read_bytes()
        expected_raw = b"".join(SSE_EVENTS)
        if raw_bytes != expected_raw:
            raise AssertionError(
                "raw.bin did not preserve the fixture stream exactly: "
                f"expected={sha256_bytes(expected_raw)} actual={sha256_bytes(raw_bytes)}"
            )
        event_records = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        event_data = [normalize_event_data(record) for record in event_records]
        expected_data = [
            '{"sequence":1,"value":"alpha"}',
            '{"sequence":2,"value":"beta"}',
            "fixture-complete",
        ]
        if event_data != expected_data:
            raise AssertionError(f"Unexpected ordered SSE events: {event_data}")
        if event_match.get("matchedEventIndex") != 2:
            raise AssertionError(
                f"Unexpected fixture-complete match metadata: {event_match}"
            )
        relative_paths = [
            str(raw_descriptor["relativePath"]),
            str(events_descriptor["relativePath"]),
            str(capture_descriptor["relativePath"]),
        ]
        if not all(
            path.startswith("experiments/stage0-toolchain/js-reverse/")
            for path in relative_paths
        ):
            raise AssertionError(f"Unexpected stream artifact namespace: {relative_paths}")
        capture_manifest = json.loads(capture_path.read_text(encoding="utf-8"))
        if capture_manifest.get("status") != "stopped":
            raise AssertionError(f"Capture did not finalize as stopped: {capture_manifest}")
        self.results.append(
            ValidationResult(
                name="SSE event sequence preservation",
                passed=True,
                evidence=[
                    "start_stream_capture was armed before the Playwright click.",
                    "raw.bin exactly matched all fixture SSE bytes in order.",
                    "events.jsonl preserved both message events and fixture-complete.",
                    "get_stream_status matched fixture-complete inside the collector.",
                    "Artifacts used the stage0-toolchain namespace and relative paths.",
                    f"raw.bin: {len(raw_bytes)} bytes, sha256={sha256_bytes(raw_bytes)}",
                ],
            )
        )
        return {"echo": echo_request_id, "sse": sse_request_id}

    async def _validate_initiator(
        self,
        client: McpToolClient,
        request_id: int,
    ) -> None:
        try:
            initiator = await client.call(
                "get_request_initiator",
                {"requestId": request_id},
            )
            text = flatten_text(initiator)
            if "app.js" not in text or "sendEcho" not in text:
                raise AssertionError(f"Expected app.js/sendEcho in initiator evidence: {initiator}")
            self.results.append(
                ValidationResult(
                    name="Request initiator",
                    passed=True,
                    evidence=["Initiator stack identifies app.js and sendEcho()."],
                )
            )
        except Exception as exc:
            self.results.append(
                ValidationResult(
                    name="Request initiator",
                    passed=False,
                    error=str(exc),
                )
            )
            raise

    async def _validate_sources(
        self,
        client: McpToolClient,
        fixture_url: str,
    ) -> None:
        try:
            search = await client.call(
                "search_in_sources",
                {
                    "query": "buildEchoRequest",
                    "caseSensitive": True,
                    "urlFilter": "app.js",
                    "maxResults": 10,
                },
            )
            search_text = flatten_text(search)
            if "buildEchoRequest" not in search_text or "app.js" not in search_text:
                raise AssertionError(f"Source search did not find fixture function: {search}")

            source = await client.call(
                "get_script_source",
                {
                    "url": f"{fixture_url.rstrip('/')}/app.js",
                    "startLine": 1,
                    "endLine": 30,
                },
            )
            source_text = flatten_text(source)
            if (
                "buildEchoRequest" not in source_text
                or "SYNTHETIC_SOURCE_MARKER" not in source_text
            ):
                raise AssertionError(f"Source read did not contain expected markers: {source}")

            self.results.append(
                ValidationResult(
                    name="Script read and search",
                    passed=True,
                    evidence=[
                        "search_in_sources located buildEchoRequest in app.js.",
                        "get_script_source returned the expected source marker.",
                    ],
                )
            )
        except Exception as exc:
            self.results.append(
                ValidationResult(
                    name="Script read and search",
                    passed=False,
                    error=str(exc),
                )
            )
            raise

    async def _validate_xhr_breakpoint(self, client: McpToolClient) -> None:
        pattern = "/api/echo"
        click_process: subprocess.Popen[str] | None = None
        try:
            await client.call("break_on_xhr", {"url": pattern})
            click_process = self.playwright.start_button_click("send-echo", "Send echo")

            paused_payload: dict[str, Any] | None = None
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                try:
                    payload = await client.call(
                        "get_paused_info",
                        {"includeScopes": False, "frameIndex": 0},
                    )
                except RuntimeError as exc:
                    if "PRECONDITION_FAILED" in str(exc) and "not paused" in str(exc):
                        await asyncio.sleep(0.2)
                        continue
                    raise
                data = payload.get("data")
                if isinstance(data, dict) and data.get("paused") is True:
                    paused_payload = payload
                    break
                await asyncio.sleep(0.2)
            if paused_payload is None:
                raise TimeoutError("XHR/fetch breakpoint did not pause execution.")

            paused_data = paused_payload.get("data")
            if not isinstance(paused_data, dict):
                raise AssertionError(
                    f"Paused payload did not include structured data: {paused_payload}"
                )
            call_frames = paused_data.get("callFrames")
            top_frame = call_frames[0] if isinstance(call_frames, list) and call_frames else None
            if paused_data.get("reason") != "XHR" or not isinstance(top_frame, dict):
                raise AssertionError(f"Unexpected pause reason or call frame: {paused_payload}")
            if top_frame.get("functionName") != "sendEcho":
                raise AssertionError(f"XHR pause did not occur in sendEcho: {paused_payload}")

            await client.call("pause_or_resume", {"action": "resume"})
            stdout, stderr = click_process.communicate(timeout=20.0)
            if click_process.returncode not in (0, None):
                raise RuntimeError(f"Playwright click failed after resume: {stderr or stdout}")

            await client.call(
                "remove_breakpoint",
                {"action": "remove_xhr", "url": pattern, "confirm": True},
            )
            breakpoints = await client.call("list_breakpoints", {"pageSize": 20})
            if pattern in flatten_text(breakpoints):
                raise AssertionError(f"XHR breakpoint remained after removal: {breakpoints}")

            self.results.append(
                ValidationResult(
                    name="XHR/fetch breakpoint pause and resume",
                    passed=True,
                    evidence=[
                        "Future fetch to /api/echo paused with reason XHR in sendEcho().",
                        "Execution resumed successfully.",
                        "XHR breakpoint was removed after the check.",
                    ],
                )
            )
        except Exception as exc:
            try:
                await client.call("pause_or_resume", {"action": "resume"})
            except Exception:
                pass
            try:
                await client.call(
                    "remove_breakpoint",
                    {"action": "remove_xhr", "url": pattern, "confirm": True},
                )
            except Exception:
                pass
            if click_process is not None and click_process.poll() is None:
                stop_text_process(click_process)
            self.results.append(
                ValidationResult(
                    name="XHR/fetch breakpoint pause and resume",
                    passed=False,
                    error=str(exc),
                )
            )
            raise

    def _validate_local_evidence_write(self) -> None:
        name = "Local evidence file write"
        probe_path = self.report_path.parent / ".toolchain-validation-write-probe"
        marker = "workspace-write-ok\n"
        try:
            probe_path.write_text(marker, encoding="utf-8", newline="\n")
            observed = probe_path.read_text(encoding="utf-8")
            if observed != marker:
                raise AssertionError("Local evidence write/read marker mismatch.")
            self.results.append(
                ValidationResult(
                    name=name,
                    passed=True,
                    evidence=[
                        "Python wrote and read back a UTF-8 file in the "
                        "Action-local evidence directory."
                    ],
                )
            )
        except Exception as exc:
            self.results.append(ValidationResult(name=name, passed=False, error=str(exc)))
        finally:
            probe_path.unlink(missing_ok=True)

    def _ensure_required_result_rows(self) -> None:
        required_names = [
            "Playwright current page aligns with js-reverse target",
            "Network request capture",
            "Request and response body export",
            "SSE event sequence preservation",
            "Request initiator",
            "Script read and search",
            "XHR/fetch breakpoint pause and resume",
            "Local evidence file write",
        ]
        existing = {result.name for result in self.results}
        for name in required_names:
            if name not in existing:
                self.results.append(
                    ValidationResult(
                        name=name,
                        passed=False,
                        error="Not reached because an earlier validation step failed.",
                    )
                )

    def _write_report(self) -> None:
        ordered_names = [
            "Playwright current page aligns with js-reverse target",
            "Network request capture",
            "Request and response body export",
            "SSE event sequence preservation",
            "Request initiator",
            "Script read and search",
            "XHR/fetch breakpoint pause and resume",
            "Local evidence file write",
        ]
        result_by_name = {result.name: result for result in self.results}
        ordered_results = [result_by_name[name] for name in ordered_names]
        extras = [result for result in self.results if result.name not in ordered_names]
        all_required_passed = all(result.passed for result in ordered_results)
        reproduction_command = "python tools/toolchain_validation.py"
        if self.js_reverse_entry is not None:
            reproduction_command += (
                " --js-reverse-entry "
                "<path-to-js-reverse-mcp>/build/src/index.js"
            )

        lines = [
            "# Stage 0 Toolchain Validation",
            "",
            "## Scope",
            "",
            "This report validates the existing toolchain for the minimum browser-analysis loop.",
            "The shared CDP endpoint is treated as a confirmed prerequisite and is not itself",
            "evaluated. The run used an isolated local fixture and a loopback-only browser",
            "session.",
            "No cookie, token, browser profile path, or CDP endpoint is recorded here.",
            "",
            "## Reproduction",
            "",
            "```powershell",
            reproduction_command,
            "```",
            "",
            "The fixture application is stored under",
            "`tests/fixtures/toolchain_validation/`; JavaScript is loaded from `app.js` rather",
            "than constructed inline by the validation runner.",
            "",
            "## Environment",
            "",
            f"- Python: `{self.environment.get('python', 'unknown')}`",
            f"- playwright-cli: `{self.environment.get('playwright-cli', 'unknown')}`",
            f"- js-reverse-mcp: `{self.environment.get('js-reverse-mcp', 'unknown')}`",
            "- Browser: system Google Chrome, isolated temporary profile, headless mode",
            "",
            "## Results",
            "",
            "| Requirement | Result |",
            "| --- | --- |",
        ]
        for result in ordered_results:
            lines.append(f"| {result.name} | **{result.status}** |")

        lines.extend(["", "## Evidence", ""])
        for result in ordered_results:
            lines.append(f"### {result.name}: {result.status}")
            lines.append("")
            if result.evidence:
                lines.extend(f"- {item}" for item in result.evidence)
            if result.error:
                lines.append(f"- Error: `{sanitize_markdown(result.error)}`")
            lines.append("")

        if extras:
            lines.extend(["## Harness diagnostics", ""])
            for result in extras:
                lines.append(f"- {result.status}: {result.name}")
                if result.error:
                    lines.append(f"  - `{sanitize_markdown(result.error)}`")
            lines.append("")

        lines.extend(
            [
                "## Conclusion",
                "",
                (
                    "All required Stage 0 checks passed. The toolchain now validates the actual "
                    "Raw Stream Capture lifecycle: the collector is armed before the browser "
                    "action, exact raw bytes and ordered semantic events are written under an "
                    "experiment namespace, the fixture-complete predicate is matched inside "
                    "the collector, "
                    "and the capture finalizes with relative artifact paths."
                    if all_required_passed
                    else
                    "At least one required Stage 0 check failed. Do not treat the browser "
                    "toolchain as ready until the failing fixture evidence is corrected."
                ),
                "",
                "## Limitations",
                "",
                "- The SSE check covers a normally completed local EventSource stream with two",
                "  ordered data events and a `fixture-complete` marker.",
                "- It validates exact normal-completion bytes, event ordering, namespace,",
                "  collector-side predicate matching, and finalization.",
                "- Cancellation, network interruption, heartbeats, and incomplete streams remain",
                "  later experiment-specific validation cases.",
                "- The test page runs in the main page target; Worker and Service Worker metadata",
                "  remain outside this Stage 0 acceptance set.",
                "",
            ]
        )
        self.report_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def build_npx_command(package: str, args: Sequence[str]) -> list[str]:
    node_path = shutil.which("node")
    pinned_entry = os.environ.get("WEB_REV_PLAYWRIGHT_CLI_ENTRY", "").strip()
    if pinned_entry:
        entry = Path(pinned_entry).resolve()
        if node_path is None:
            raise RuntimeError("node must be available for WEB_REV_PLAYWRIGHT_CLI_ENTRY.")
        if not entry.is_file():
            raise RuntimeError(f"Pinned playwright-cli entry was not found: {entry}")
        return [node_path, str(entry), *args]
    npx_path = shutil.which("npx.cmd") or shutil.which("npx")
    if node_path is None or npx_path is None:
        raise RuntimeError("node and npx must both be available on PATH.")

    npx_cli = Path(npx_path).resolve().parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
    if not npx_cli.is_file():
        raise RuntimeError(f"Unable to locate npx-cli.js beside {npx_path}.")
    return [node_path, str(npx_cli), "--yes", package, *args]


def build_mcp_server_parameters(
    endpoint: str,
    repo_root: Path,
    evidence_root: Path,
    js_reverse_entry: Path | None,
) -> StdioServerParameters:
    arguments = [
        "--browserUrl",
        endpoint,
        "--allowedRoots",
        str(evidence_root),
        "--streamArtifactRoot",
        "0",
    ]
    if js_reverse_entry is None:
        executable = shutil.which(JS_REVERSE_COMMAND)
        if executable is None:
            raise RuntimeError(
                "js-reverse-mcp was not found on PATH; clone "
                "qqq694637644/js-reverse-mcp, run npm install, npm run build, "
                "and npm link"
            )
        command = [executable, *arguments]
    else:
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node is required for a local js-reverse-mcp entrypoint")
        command = [node, str(js_reverse_entry), *arguments]
    return StdioServerParameters(
        command=command[0],
        args=command[1:],
        cwd=str(repo_root),
    )


def run_command(command: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    return CommandResult(
        args=tuple(command),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_chrome() -> Path:
    candidates = [
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Google Chrome executable was not found.")


def start_chrome(port: int, profile_path: Path) -> subprocess.Popen[bytes]:
    chrome = find_chrome()
    args = [
        str(chrome),
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_path}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "about:blank",
    ]
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=1.0) as response:
                if 200 <= response.status < 300:
                    return
        except Exception as exc:
            last_error = exc
            time.sleep(0.2)
    raise TimeoutError(f"HTTP endpoint did not become ready: {last_error}")


def stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5.0)


def stop_text_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.communicate(timeout=5.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5.0)


def model_to_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(by_alias=True, exclude_none=True)
        if isinstance(dumped, dict):
            return dumped
    if isinstance(value, dict):
        return value
    raise TypeError(f"Unsupported MCP result type: {type(value)!r}")


def parse_tool_text_content(payload: dict[str, Any]) -> dict[str, Any]:
    for content in payload.get("content", []):
        if not isinstance(content, dict) or content.get("type") != "text":
            continue
        text = content.get("text")
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError(f"MCP result did not contain structured JSON: {payload}")


def walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for nested in value.values():
            yield from walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk(nested)


def flatten_text(value: Any) -> str:
    parts: list[str] = []
    for item in walk(value):
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, (int, float, bool)):
            parts.append(str(item))
    return "\n".join(parts)


def find_snapshot_reference(output: str, accessible_name: str) -> str | None:
    candidates = [output]
    try:
        payload = json.loads(output)
        candidates.extend(item for item in walk(payload) if isinstance(item, str))
    except json.JSONDecodeError:
        pass
    pattern = re.compile(
        rf"button[^\n]*{re.escape(accessible_name)}[^\n]*\[ref=([^\]]+)\]",
        re.IGNORECASE,
    )
    for candidate in candidates:
        match = pattern.search(candidate)
        if match:
            return match.group(1)
    return None


def extract_page_metadata(raw_text: str, expected_url: str) -> dict[str, str]:
    parsed: Any
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = raw_text

    expected = normalize_url(expected_url)
    for item in walk(parsed):
        if not isinstance(item, dict):
            continue
        url = first_string(item, ["url", "pageUrl", "pageURL", "targetUrl", "targetURL"])
        title = first_string(item, ["title", "pageTitle", "targetTitle"])
        if url and normalize_url(url) == expected:
            return {"url": url, "title": title or "Stage 0 Toolchain Validation"}

    text = flatten_text(parsed)
    if expected_url in text and "Stage 0 Toolchain Validation" in text:
        return {"url": expected_url, "title": "Stage 0 Toolchain Validation"}
    raise AssertionError(f"Unable to extract page metadata for {expected_url}: {raw_text}")


def extract_page_index(payload: dict[str, Any], expected_url: str) -> int | None:
    expected = normalize_url(expected_url)
    for item in walk(payload):
        if not isinstance(item, dict):
            continue
        url = first_string(item, ["url", "pageUrl", "pageURL", "targetUrl", "targetURL"])
        if not url or normalize_url(url) != expected:
            continue
        for key in ("pageIdx", "pageIndex", "index"):
            value = item.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
    return None


def extract_request_id(payload: dict[str, Any], url_fragment: str) -> int:
    for item in walk(payload):
        if not isinstance(item, dict):
            continue
        url = first_string(item, ["url", "requestUrl", "requestURL"])
        if not url or url_fragment not in url:
            continue
        for key in ("reqid", "requestId", "id"):
            value = item.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, float) and value.is_integer():
                return int(value)
    raise AssertionError(f"Unable to find request ID for {url_fragment}: {payload}")


def extract_integer(payload: dict[str, Any], key: str) -> int:
    for item in walk(payload):
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
    raise AssertionError(f"Unable to find integer {key!r}: {payload}")


def find_mapping(
    payload: dict[str, Any], key: str, expected: object
) -> dict[str, Any] | None:
    for item in walk(payload):
        if isinstance(item, dict) and item.get(key) == expected:
            return item
    return None


def extract_artifact(payload: dict[str, Any], kind: str) -> dict[str, Any]:
    for item in walk(payload):
        if not isinstance(item, dict) or item.get("kind") != kind:
            continue
        if isinstance(item.get("relativePath"), str):
            return item
    raise AssertionError(f"Unable to find artifact kind {kind!r}: {payload}")


def resolve_artifact(root: Path, descriptor: dict[str, Any]) -> Path:
    relative_path = descriptor.get("relativePath")
    if not isinstance(relative_path, str):
        raise AssertionError(f"Artifact has no relativePath: {descriptor}")
    candidate = (root / Path(relative_path)).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise AssertionError(f"Artifact escaped evidence root: {descriptor}") from exc
    if not candidate.is_file():
        raise AssertionError(f"Artifact file is missing: {candidate}")
    return candidate


def normalize_event_data(record: dict[str, Any]) -> str | None:
    data = record.get("data")
    if isinstance(data, str):
        return data
    data_json = record.get("dataJson")
    if data_json is not None:
        return json.dumps(data_json, separators=(",", ":"), ensure_ascii=False)
    return None


def first_string(payload: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def normalize_url(url: str) -> str:
    return url.rstrip("/")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize_markdown(value: str) -> str:
    return value.replace("`", "'").replace("\r", " ").replace("\n", " ")[:800]


async def async_main(repo_root: Path, js_reverse_entry: Path | None) -> int:
    validation = Stage0Validation(repo_root, js_reverse_entry)
    return await validation.run()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the Stage 0 browser-analysis toolchain.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root. Defaults to the parent of tools/.",
    )
    parser.add_argument(
        "--js-reverse-entry",
        type=Path,
        default=None,
        help=(
            "Optional local built qqq694637644/js-reverse-mcp entrypoint. "
            "Defaults to the npm-linked js-reverse-mcp command on PATH; "
            "for example build/src/index.js."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(
        async_main(
            args.repo_root.resolve(),
            args.js_reverse_entry.resolve() if args.js_reverse_entry else None,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
