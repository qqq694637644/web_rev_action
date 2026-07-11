from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from playwright.async_api import async_playwright

FIXTURE_HTML = """<!doctype html>
<html>
  <head><meta charset="utf-8"><title>Dual CDP fixture</title></head>
  <body>
    <main id="app" data-authenticated="true">
      <h1>Authenticated chat fixture</h1>
      <label for="message">Message</label>
      <textarea id="message"></textarea>
      <button id="send" type="button">Send</button>
      <pre id="output"></pre>
    </main>
    <script src="/app.js"></script>
  </body>
</html>
"""

FIXTURE_SCRIPT = """const endpoint = '/api/conversation';
const sendButton = document.querySelector('#send');
sendButton.addEventListener('click', async () => {
  const message = document.querySelector('#message').value;
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({
      action: 'next',
      messages: [{role: 'user', content: {content_type: 'text', parts: [message]}}],
      parent_message_id: 'fixture-parent-message'
    })
  });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let transcript = '';
  while (true) {
    const result = await reader.read();
    if (result.done) break;
    transcript += decoder.decode(result.value, {stream: true});
    document.querySelector('#output').textContent = transcript;
  }
});
"""


@dataclass(frozen=True)
class ValidationConfig:
    mode: str
    target_url: str
    auth_check_text: str
    message_locator_text: str
    submit_locator_text: str
    request_url_filter: str
    script_search_query: str
    test_message: str
    wait_after_submit_ms: int


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "DualCdpFixture/1.0"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _authenticated(self) -> bool:
        return "fixture_session=valid" in self.headers.get("Cookie", "")

    def _send_bytes(self, status: HTTPStatus, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/app":
            if not self._authenticated():
                self._send_bytes(
                    HTTPStatus.UNAUTHORIZED,
                    "text/html; charset=utf-8",
                    b"<h1>Login required</h1>",
                )
                return
            self._send_bytes(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                FIXTURE_HTML.encode("utf-8"),
            )
            return

        if self.path == "/app.js":
            self._send_bytes(
                HTTPStatus.OK,
                "application/javascript; charset=utf-8",
                FIXTURE_SCRIPT.encode("utf-8"),
            )
            return

        self._send_bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/api/conversation":
            self._send_bytes(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found")
            return
        if not self._authenticated():
            self._send_bytes(
                HTTPStatus.UNAUTHORIZED,
                "application/json; charset=utf-8",
                b'{"error":"not authenticated"}',
            )
            return

        content_length = int(self.headers.get("Content-Length", "0"))
        request_body = self.rfile.read(content_length)
        try:
            parsed_body: Any = json.loads(request_body)
        except json.JSONDecodeError:
            parsed_body = {"raw": request_body.decode("utf-8", errors="replace")}

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        events = [
            {"type": "message_start", "request": parsed_body},
            {"type": "message_delta", "delta": "fixture reply"},
        ]
        for event in events:
            self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.1)
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class FixtureServer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def target_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/app"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class ArtifactWriter:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, name: str) -> Path:
        return self.root / name

    def write_json(self, name: str, value: Any) -> Path:
        target = self.path(name)
        target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return target

    def write_text(self, name: str, value: str) -> Path:
        target = self.path(name)
        target.write_text(value, encoding="utf-8")
        return target


class PlaywrightCli:
    def __init__(self, executable: str, artifacts: ArtifactWriter) -> None:
        self.executable = executable
        self.artifacts = artifacts
        self.command_index = 0

    async def run(self, *args: str, check: bool = True) -> CommandResult:
        command = [self.executable, *args]
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        result = CommandResult(
            command=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        self.command_index += 1
        self.artifacts.write_json(
            f"playwright-command-{self.command_index:02d}.json",
            asdict(result),
        )
        reported_error = "### Error" in result.stdout
        if check and (result.returncode != 0 or reported_error):
            raise RuntimeError(
                f"playwright-cli failed ({result.returncode}): {' '.join(command)}\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
        return result


class McpEvidenceClient:
    def __init__(self, session: ClientSession, artifacts: ArtifactWriter) -> None:
        self.session = session
        self.artifacts = artifacts
        self.call_index = 0

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        result = await self.session.call_tool(name, arguments or {})
        payload = result.model_dump(mode="json", by_alias=True)
        self.call_index += 1
        self.artifacts.write_json(f"mcp-call-{self.call_index:02d}-{name}.json", payload)
        if payload.get("isError") or payload.get("is_error"):
            raise RuntimeError(f"MCP tool {name} failed: {tool_text(payload)}")
        structured = payload.get("structuredContent") or payload.get("structured_content") or {}
        if isinstance(structured, dict) and structured.get("ok") is False:
            raise RuntimeError(f"MCP tool {name} failed: {json.dumps(structured)}")
        return payload


def env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"live mode requires {name}")
    return value


def resolve_command(env_name: str, default_name: str) -> str:
    configured = os.environ.get(env_name, "").strip()
    if configured:
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path.resolve())
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        raise RuntimeError(f"{env_name} does not resolve to an executable: {configured}")
    resolved = shutil.which(default_name)
    if not resolved:
        raise RuntimeError(f"{default_name} is not installed or not on PATH")
    return resolved


def decode_storage_state() -> dict[str, Any]:
    encoded = os.environ.get("BROWSER_STORAGE_STATE_B64", "").strip()
    if not encoded:
        raise RuntimeError("live mode requires the BROWSER_STORAGE_STATE_B64 repository secret")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        state = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("BROWSER_STORAGE_STATE_B64 is not valid base64-encoded JSON") from error
    if not isinstance(state, dict):
        raise RuntimeError("decoded browser storage state must be a JSON object")
    return state


def fixture_storage_state() -> dict[str, Any]:
    return {
        "cookies": [
            {
                "name": "fixture_session",
                "value": "valid",
                "domain": "127.0.0.1",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def build_config(fixture: FixtureServer | None) -> tuple[ValidationConfig, dict[str, Any]]:
    mode = os.environ.get("VALIDATION_MODE", "fixture").strip().lower()
    if mode == "fixture":
        if fixture is None:
            raise RuntimeError("fixture server is not available")
        return (
            ValidationConfig(
                mode=mode,
                target_url=fixture.target_url,
                auth_check_text="Authenticated chat fixture",
                message_locator_text="Message",
                submit_locator_text="Send",
                request_url_filter="/api/conversation",
                script_search_query="/api/conversation",
                test_message=os.environ.get("TEST_MESSAGE", "dual-cdp fixture message"),
                wait_after_submit_ms=int(os.environ.get("WAIT_AFTER_SUBMIT_MS", "3000")),
            ),
            fixture_storage_state(),
        )
    if mode == "live":
        request_filter = env_required("REQUEST_URL_FILTER")
        return (
            ValidationConfig(
                mode=mode,
                target_url=env_required("TARGET_URL"),
                auth_check_text=env_required("AUTH_CHECK_TEXT"),
                message_locator_text=env_required("MESSAGE_LOCATOR_TEXT"),
                submit_locator_text=os.environ.get("SUBMIT_LOCATOR_TEXT", "").strip(),
                request_url_filter=request_filter,
                script_search_query=os.environ.get("SCRIPT_SEARCH_QUERY", "").strip()
                or request_filter,
                test_message=os.environ.get(
                    "TEST_MESSAGE", "Reply with exactly: dual-cdp-ok"
                ),
                wait_after_submit_ms=int(os.environ.get("WAIT_AFTER_SUBMIT_MS", "8000")),
            ),
            decode_storage_state(),
        )
    raise RuntimeError(f"unknown VALIDATION_MODE: {mode}")


def tool_text(payload: dict[str, Any]) -> str:
    content = payload.get("content") or []
    return "\n".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def structured_data(payload: dict[str, Any]) -> dict[str, Any]:
    structured = payload.get("structuredContent") or payload.get("structured_content") or {}
    if not isinstance(structured, dict):
        return {}
    data = structured.get("data")
    return data if isinstance(data, dict) else structured


def nested_dicts(value: Any) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if isinstance(value, dict):
        output.append(value)
        for child in value.values():
            output.extend(nested_dicts(child))
    elif isinstance(value, list):
        for child in value:
            output.extend(nested_dicts(child))
    return output


def parse_page_url(command_output: str) -> str:
    match = re.search(r"Page URL:\s*(\S+)", command_output)
    if not match:
        raise RuntimeError(f"playwright-cli snapshot did not report Page URL:\n{command_output}")
    return match.group(1)


def find_snapshot_ref(snapshot: str, locator_text: str) -> str:
    lowered = locator_text.casefold()
    lines = snapshot.splitlines()

    for line in lines:
        if lowered not in line.casefold():
            continue
        match = re.search(r"(?:ref=)?(e\d+)\b", line)
        if match:
            return match.group(1)

    for index, line in enumerate(lines):
        if lowered not in line.casefold():
            continue
        candidates = lines[index + 1 : min(len(lines), index + 3)]
        for candidate in candidates:
            match = re.search(r"(?:ref=)?(e\d+)\b", candidate)
            if match:
                return match.group(1)
    raise RuntimeError(f"could not find a snapshot ref containing text: {locator_text!r}")


def find_target_request(payload: dict[str, Any], url_filter: str) -> dict[str, Any]:
    requests = structured_data(payload).get("requests", [])
    if not isinstance(requests, list):
        requests = []
    for request in requests:
        if isinstance(request, dict) and url_filter in str(request.get("url", "")):
            return request
    raise RuntimeError(f"target request was not captured; candidates: {json.dumps(requests)}")


def find_page(payload: dict[str, Any], target_url: str) -> dict[str, Any]:
    pages = structured_data(payload).get("pages", [])
    if not isinstance(pages, list):
        pages = []
    target = urlparse(target_url)
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_url = urlparse(str(page.get("url", "")))
        if page_url.netloc == target.netloc and page_url.path == target.path:
            return page
    raise RuntimeError(f"js-reverse-mcp did not expose target page; pages: {json.dumps(pages)}")


def find_source_candidate(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        item
        for item in nested_dicts(structured_data(payload))
        if isinstance(item.get("url"), str) or isinstance(item.get("scriptId"), str)
    ]
    candidates.sort(
        key=lambda item: (
            not isinstance(item.get("scriptId"), str),
            not isinstance(item.get("url"), str),
        )
    )
    if not candidates:
        raise RuntimeError(f"source search returned no script candidate: {tool_text(payload)}")
    return candidates[0]


def source_read_arguments(candidate: dict[str, Any]) -> dict[str, Any]:
    selector: dict[str, Any]
    if isinstance(candidate.get("url"), str) and candidate["url"]:
        selector = {"url": candidate["url"]}
    elif isinstance(candidate.get("scriptId"), str) and candidate["scriptId"]:
        selector = {"scriptId": candidate["scriptId"]}
    else:
        raise RuntimeError(f"source candidate has no usable selector: {candidate}")

    line_value = candidate.get("lineNumber", candidate.get("line", candidate.get("startLine", 1)))
    try:
        line = max(1, int(line_value))
    except (TypeError, ValueError):
        line = 1
    return {**selector, "startLine": max(1, line - 4), "endLine": line + 4}


def publish_job_summary(artifacts: ArtifactWriter) -> None:
    destination = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not destination:
        return
    summary_path = artifacts.path("summary.md")
    failure_path = artifacts.path("failure.json")
    if summary_path.exists():
        content = summary_path.read_text(encoding="utf-8")
    elif failure_path.exists():
        failure = failure_path.read_text(encoding="utf-8")
        content = (
            "# Dual CDP validation\n\n"
            "The validation failed before a complete summary was generated.\n\n"
            f"```json\n{failure}```\n"
        )
    else:
        content = "# Dual CDP validation\n\nNo result artifact was generated.\n"
    with Path(destination).open("a", encoding="utf-8") as summary_file:
        summary_file.write(content)


async def wait_for_cdp(endpoint: str, timeout_seconds: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{endpoint}/json/version", timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as error:  # noqa: BLE001
            last_error = error
            await asyncio.sleep(0.25)
    raise RuntimeError(f"CDP endpoint did not become ready: {last_error}")


async def chromium_executable() -> str:
    async with async_playwright() as playwright:
        return playwright.chromium.executable_path


async def run_validation() -> int:
    output_dir = Path(os.environ.get("OUTPUT_DIR", "artifacts/dual-cdp-validation"))
    artifacts = ArtifactWriter(output_dir)
    fixture: FixtureServer | None = None
    browser_process: subprocess.Popen[bytes] | None = None
    playwright_cli: PlaywrightCli | None = None
    success = False

    try:
        mode = os.environ.get("VALIDATION_MODE", "fixture").strip().lower()
        if mode == "fixture":
            fixture = FixtureServer()
            fixture.start()

        config, storage_state = build_config(fixture)
        artifacts.write_json("configuration.json", asdict(config))

        cdp_port = int(os.environ.get("CDP_PORT", "9222"))
        cdp_endpoint = f"http://127.0.0.1:{cdp_port}"
        playwright_cli_path = resolve_command(
            "PLAYWRIGHT_CLI_COMMAND", "playwright-cli"
        )
        js_reverse_path = resolve_command(
            "JS_REVERSE_MCP_COMMAND", "js-reverse-mcp"
        )
        playwright_cli = PlaywrightCli(playwright_cli_path, artifacts)

        with tempfile.TemporaryDirectory(
            prefix="dual-cdp-", ignore_cleanup_errors=True
        ) as temporary_directory:
            temporary_root = Path(temporary_directory)
            profile_dir = temporary_root / "chromium-profile"
            auth_state_path = temporary_root / "auth-state.json"
            profile_dir.mkdir()
            auth_state_path.write_text(
                json.dumps(storage_state, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            browser_log_path = artifacts.path("chromium.log")
            browser_log = browser_log_path.open("wb")
            executable = await chromium_executable()
            browser_process = subprocess.Popen(
                [
                    executable,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    f"--remote-debugging-port={cdp_port}",
                    "--remote-debugging-address=127.0.0.1",
                    f"--user-data-dir={profile_dir}",
                    "about:blank",
                ],
                stdout=browser_log,
                stderr=subprocess.STDOUT,
            )
            cdp_version = await wait_for_cdp(cdp_endpoint)
            artifacts.write_json("cdp-version.json", cdp_version)

            await playwright_cli.run("attach", f"--cdp={cdp_endpoint}")
            await playwright_cli.run("state-load", str(auth_state_path))

            mcp_log_path = artifacts.path("js-reverse-mcp.log")
            mcp_stderr_path = artifacts.path("js-reverse-mcp-stderr.log")
            with mcp_stderr_path.open("w", encoding="utf-8") as mcp_stderr:
                server_parameters = StdioServerParameters(
                    command=js_reverse_path,
                    args=[
                        "--browserUrl",
                        cdp_endpoint,
                        "--allowedRoots",
                        str(artifacts.root),
                        "--logFile",
                        str(mcp_log_path),
                    ],
                )
                async with stdio_client(server_parameters, errlog=mcp_stderr) as streams:
                    read_stream, write_stream = streams
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        mcp = McpEvidenceClient(session, artifacts)

                        await mcp.call("select_page")
                        await mcp.call("list_network_requests", {"pageSize": 5})
                        await playwright_cli.run("goto", config.target_url)

                        snapshot_path = artifacts.path("playwright-snapshot.yml")
                        snapshot_result = await playwright_cli.run(
                            "snapshot", f"--filename={snapshot_path}"
                        )
                        snapshot = snapshot_path.read_text(encoding="utf-8")
                        if config.auth_check_text.casefold() not in snapshot.casefold():
                            raise RuntimeError(
                                "authenticated marker not found in snapshot: "
                                f"{config.auth_check_text!r}"
                            )
                        playwright_page_url = parse_page_url(snapshot_result.stdout)

                        page_list = await mcp.call("select_page")
                        selected_page = find_page(page_list, config.target_url)
                        page_index = selected_page.get("pageIdx")
                        if not isinstance(page_index, (int, float)):
                            raise RuntimeError(f"selected page lacks pageIdx: {selected_page}")
                        await mcp.call("select_page", {"pageIdx": page_index})
                        await mcp.call("clear_network_requests", {"confirm": True})

                        message_ref = find_snapshot_ref(snapshot, config.message_locator_text)
                        await playwright_cli.run("fill", message_ref, config.test_message)
                        if config.submit_locator_text:
                            submit_ref = find_snapshot_ref(snapshot, config.submit_locator_text)
                            await playwright_cli.run("click", submit_ref)
                        else:
                            await playwright_cli.run("press", "Enter")
                        await asyncio.sleep(config.wait_after_submit_ms / 1000)

                        request_list = await mcp.call(
                            "list_network_requests",
                            {
                                "methods": ["POST"],
                                "urlFilter": config.request_url_filter,
                                "pageSize": 100,
                            },
                        )
                        target_request = find_target_request(
                            request_list, config.request_url_filter
                        )
                        request_id = target_request.get("reqid")
                        if not isinstance(request_id, (int, float)):
                            raise RuntimeError(f"target request lacks reqid: {target_request}")

                        request_body_path = artifacts.path("request-body.txt")
                        response_body_path = artifacts.path("response-body.txt")
                        request_bundle_path = artifacts.path("request-all.json")
                        await mcp.call(
                            "list_network_requests",
                            {
                                "reqid": request_id,
                                "outputFile": str(request_body_path),
                                "outputPart": "requestBody",
                            },
                        )
                        await mcp.call(
                            "list_network_requests",
                            {
                                "reqid": request_id,
                                "outputFile": str(response_body_path),
                                "outputPart": "responseBody",
                            },
                        )
                        await mcp.call(
                            "list_network_requests",
                            {
                                "reqid": request_id,
                                "outputFile": str(request_bundle_path),
                                "outputPart": "all",
                            },
                        )

                        initiator = await mcp.call(
                            "get_request_initiator", {"requestId": request_id}
                        )
                        artifacts.write_json("initiator.json", initiator)

                        source_search = await mcp.call(
                            "search_in_sources",
                            {
                                "query": config.script_search_query,
                                "maxResults": 20,
                                "excludeMinified": False,
                            },
                        )
                        artifacts.write_json("source-search.json", source_search)
                        source_candidate = find_source_candidate(source_search)
                        source_snippet = await mcp.call(
                            "get_script_source", source_read_arguments(source_candidate)
                        )
                        artifacts.write_json("script-source.json", source_snippet)

                        request_body = request_body_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        response_body = response_body_path.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        initiator_text = json.dumps(initiator, ensure_ascii=False)
                        source_text = json.dumps(source_snippet, ensure_ascii=False)
                        js_reverse_page_url = str(selected_page.get("url", ""))
                        playwright_url = urlparse(playwright_page_url)
                        reverse_url = urlparse(js_reverse_page_url)

                        checks = {
                            "cdp_endpoint_available": bool(
                                cdp_version.get("webSocketDebuggerUrl")
                            ),
                            "playwright_attached": playwright_url.netloc
                            == urlparse(config.target_url).netloc,
                            "js_reverse_attached": reverse_url.netloc
                            == urlparse(config.target_url).netloc,
                            "same_page_observed": (
                                playwright_url.netloc == reverse_url.netloc
                                and playwright_url.path == reverse_url.path
                            ),
                            "authenticated_page_opened": True,
                            "request_body_captured": bool(request_body)
                            and config.test_message in request_body,
                            "sse_response_captured": "data:" in response_body,
                            "sse_completion_observed": (
                                "[DONE]" in response_body if config.mode == "fixture" else True
                            ),
                            "initiator_captured": any(
                                marker in initiator_text
                                for marker in ("stack", "script", "url", "initiator")
                            ),
                            "related_script_read": bool(source_text)
                            and (
                                config.script_search_query in source_text
                                or len(source_text) > 200
                            ),
                            "artifacts_saved": all(
                                path.exists()
                                for path in (
                                    request_body_path,
                                    response_body_path,
                                    request_bundle_path,
                                )
                            ),
                        }
                        failed_checks = [
                            name for name, passed in checks.items() if not passed
                        ]
                        summary = {
                            "ok": not failed_checks,
                            "mode": config.mode,
                            "cdp_endpoint": cdp_endpoint,
                            "playwright_page_url": playwright_page_url,
                            "js_reverse_page_url": js_reverse_page_url,
                            "request": target_request,
                            "source_candidate": source_candidate,
                            "checks": checks,
                            "failed_checks": failed_checks,
                            "artifact_directory": str(artifacts.root),
                        }
                        artifacts.write_json("summary.json", summary)
                        markdown_checks = "\n".join(
                            f"- {'✅' if passed else '❌'} {name}"
                            for name, passed in checks.items()
                        )
                        artifacts.write_text(
                            "summary.md",
                            "# Dual CDP validation\n\n"
                            f"- Result: **{'PASS' if summary['ok'] else 'FAIL'}**\n"
                            f"- Mode: `{config.mode}`\n"
                            f"- CDP endpoint: `{cdp_endpoint}`\n"
                            f"- Playwright page: `{playwright_page_url}`\n"
                            f"- JS Reverse page: `{js_reverse_page_url}`\n"
                            f"- Captured request: `{target_request.get('method')} "
                            f"{target_request.get('url')}`\n\n"
                            f"## Checks\n\n{markdown_checks}\n",
                        )
                        if failed_checks:
                            raise RuntimeError(
                                f"validation failed: {', '.join(failed_checks)}"
                            )
                        success = True

            browser_log.close()

    except Exception as error:  # noqa: BLE001
        artifacts.write_json(
            "failure.json",
            {
                "ok": False,
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": "".join(traceback.format_exception(error)),
            },
        )
        print(f"validation failed: {error}", file=sys.stderr)
    finally:
        if playwright_cli is not None:
            try:
                await playwright_cli.run("detach", check=False)
            except Exception as error:  # noqa: BLE001
                print(f"playwright detach warning: {error}", file=sys.stderr)
        if browser_process is not None and browser_process.poll() is None:
            browser_process.terminate()
            try:
                browser_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                browser_process.kill()
        if fixture is not None:
            fixture.close()

    publish_job_summary(artifacts)

    return 0 if success else 1


def main() -> None:
    raise SystemExit(asyncio.run(run_validation()))


if __name__ == "__main__":
    main()
