from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.browser.adapters.command import SubprocessCommandRunner
from skill_temple.browser.adapters.contracts import (
    AdapterError,
    StreamCheckpoint,
    StreamRequestCheckpoint,
    StreamWaitResult,
)
from skill_temple.browser.adapters.js_reverse import JsReverseMcpAdapter
from skill_temple.browser.adapters.mcp import StdioMcpToolTransport
from skill_temple.browser.adapters.playwright import (
    PlaywrightCliAdapter,
    build_playwright_attach_args,
)
from skill_temple.browser_models import (
    ExactDataPredicate,
    Locator,
    RequestMatcher,
    WaitCondition,
)
from skill_temple.browser_service import (
    BrowserActionService,
    Deadline,
    ExperimentStore,
    build_browser_service_from_environment,
)
from tests.browser.common import BrowserActionTestCase
from tests.fakes.browser import FakeJsReverse, FakePlaywright


class TransportsBrowserTests(BrowserActionTestCase):
    def test_environment_builder_binds_mcp_to_workspace_and_same_cdp_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "WEB_REV_EVIDENCE_DIR": temp_dir,
                    "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
                    "WEB_REV_JS_REVERSE_COMMAND": "js-reverse-mcp",
                },
                clear=True,
            ):
                service = build_browser_service_from_environment()
            self.assertIsInstance(service.js_reverse, JsReverseMcpAdapter)
            transport = service.js_reverse.transport
            self.assertIsInstance(transport, StdioMcpToolTransport)
            self.assertEqual(
                transport.args,
                [
                    "--browserUrl",
                    "http://127.0.0.1:9222",
                    "--allowedRoots",
                    str(Path(temp_dir).resolve()),
                    "--streamArtifactRoot",
                    "0",
                ],
            )
            self.assertEqual(
                service.private_mcp_browser_endpoint,
                "http://127.0.0.1:9222",
            )

    def test_environment_builder_only_appends_non_conflicting_mcp_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "WEB_REV_EVIDENCE_DIR": temp_dir,
                    "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
                    "WEB_REV_JS_REVERSE_EXTRA_ARGS": '["--headless", "false"]',
                },
                clear=True,
            ):
                service = build_browser_service_from_environment()
            transport = service.js_reverse.transport
            self.assertEqual(
                transport.args,
                [
                    "--browserUrl",
                    "http://127.0.0.1:9222",
                    "--allowedRoots",
                    str(Path(temp_dir).resolve()),
                    "--streamArtifactRoot",
                    "0",
                    "--headless",
                    "false",
                ],
            )
            with patch.dict(
                os.environ,
                {
                    "WEB_REV_EVIDENCE_DIR": temp_dir,
                    "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
                    "WEB_REV_JS_REVERSE_EXTRA_ARGS": '["--browserUrl=http://bad"]',
                },
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "managed by web_rev_action"):
                    build_browser_service_from_environment()

    def test_configured_private_mcp_rejects_a_different_playwright_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            events: list[str] = []
            experiments = ExperimentStore(Path(temp_dir))
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, experiments.root),
                experiments=experiments,
                default_browser_endpoint="http://127.0.0.1:9222",
                private_mcp_browser_endpoint="http://127.0.0.1:9222",
                require_private_mcp_endpoint=True,
            )
            client = TestClient(create_app(browser_service=service))
            with client:
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "open_session",
                        "payload": {
                            "session_id": "session_one",
                            "browser_endpoint": "http://127.0.0.1:9333",
                        },
                    },
                )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"]["error"]["code"],
                "browser_endpoint_mismatch",
            )

    def test_private_js_reverse_adapter_calls_stream_primitives_with_namespace(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                self.calls.append((name, arguments))
                if name == "start_stream_capture":
                    return {"capture": {"captureId": 7, "version": 1}}
                if name == "get_stream_status":
                    payload = {
                        "capture": {"captureId": 7, "version": 2},
                        "requests": [
                            {
                                "cdpRequestId": "req-7",
                                "url": "https://example.test/api/resource",
                                "method": "POST",
                                "resourceType": "fetch",
                                "status": "finished",
                                "rawEventCount": 52,
                                "semanticEventCount": 0,
                            }
                        ],
                    }
                    if "eventPredicate" in arguments:
                        payload["eventMatch"] = {
                            "matched": True,
                            "matchedEventIndex": 51,
                            "matchedRequestId": "req-7",
                            "matchedSource": "raw-stream",
                        }
                    return payload
                if name == "stop_stream_capture":
                    return {"capture": {"captureId": 7, "status": "stopped"}}
                return {}

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory():
            transport = FakeTransport()
            adapter = JsReverseMcpAdapter(
                transport,
            )

            async def exercise() -> StreamWaitResult:
                deadline = Deadline(5_000)
                started = await adapter.start_stream_capture(
                    experiment_id="exp_private",
                    matcher=RequestMatcher(
                        url_contains="/api/resource",
                        method="POST",
                        resource_types=["fetch"],
                        mime_types=["text/event-stream"],
                    ),
                    include_in_flight=False,
                    deadline=deadline,
                )
                self.assertEqual(started["capture"]["captureId"], 7)
                waited = await adapter.wait_for_stream_condition(
                    capture_id=7,
                    request_matcher=RequestMatcher(url_contains="/api/resource", method="POST"),
                    condition=WaitCondition(
                        type="event_predicate",
                        request_matcher=RequestMatcher(url_contains="/api/resource", method="POST"),
                        predicate=ExactDataPredicate(
                            type="exact_data",
                            value="fixture-complete",
                        ),
                    ),
                    checkpoint=StreamCheckpoint(
                        version=1,
                        requests={
                            "req-7": StreamRequestCheckpoint(
                                status="streaming",
                                raw_event_index=-1,
                                semantic_event_index=-1,
                                primary_event_source="raw-stream",
                            )
                        },
                    ),
                    deadline=deadline,
                )
                await adapter.stop_stream_capture(7, deadline)
                return waited

            waited = asyncio.run(exercise())
            self.assertTrue(waited.condition_met)
            self.assertEqual(
                [name for name, _ in transport.calls],
                [
                    "start_stream_capture",
                    "get_stream_status",
                    "get_stream_status",
                    "stop_stream_capture",
                ],
            )
            start_args = transport.calls[0][1]
            self.assertEqual(start_args["artifactNamespace"], "exp_private")
            self.assertFalse(start_args["includeInFlight"])
            self.assertEqual(start_args["mimeTypes"], ["text/event-stream"])
            status_args = transport.calls[2][1]
            self.assertEqual(
                status_args["eventPredicate"],
                {"type": "exact_data", "value": "fixture-complete"},
            )
            self.assertEqual(status_args["afterEventIndex"], -1)
            self.assertEqual(waited.matched_event["matchedEventIndex"], 51)

    def test_stdio_mcp_transport_calls_a_real_mcp_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = root / "fake_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "from mcp.server.fastmcp import FastMCP",
                        "from pydantic import BaseModel",
                        "server = FastMCP('browser-test')",
                        "class Capture(BaseModel):",
                        "    captureId: int",
                        "    namespace: str",
                        "    includeInFlight: bool",
                        "class StartResult(BaseModel):",
                        "    capture: Capture",
                        "@server.tool(structured_output=True)",
                        "def start_stream_capture(",
                        "    artifactNamespace: str, includeInFlight: bool = False",
                        ") -> StartResult:",
                        "    return StartResult(capture=Capture(",
                        "        captureId=19,",
                        "        namespace=artifactNamespace,",
                        "        includeInFlight=includeInFlight,",
                        "    ))",
                        "if __name__ == '__main__':",
                        "    server.run(transport='stdio')",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            transport = StdioMcpToolTransport(
                command=sys.executable,
                args=[str(server)],
                cwd=root,
            )

            async def exercise() -> dict[str, Any]:
                try:
                    return await transport.call_tool(
                        "start_stream_capture",
                        {
                            "artifactNamespace": "exp_stdio",
                            "includeInFlight": False,
                        },
                        Deadline(10_000),
                    )
                finally:
                    await asyncio.create_task(transport.close())

            result = asyncio.run(exercise())
            self.assertEqual(result["capture"]["captureId"], 19)
            self.assertEqual(result["capture"]["namespace"], "exp_stdio")

    def test_expired_queued_side_effect_is_discarded_and_worker_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = root / "slow_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "import asyncio",
                        "from pathlib import Path",
                        "from mcp.server.fastmcp import FastMCP",
                        "from pydantic import BaseModel",
                        "server = FastMCP('slow-browser-test')",
                        "class Capture(BaseModel):",
                        "    captureId: int",
                        "    namespace: str",
                        "class StartResult(BaseModel):",
                        "    capture: Capture",
                        "@server.tool(structured_output=True)",
                        "async def start_stream_capture(",
                        "    artifactNamespace: str,",
                        "    marker: str,",
                        "    delayMs: int = 0,",
                        ") -> StartResult:",
                        "    await asyncio.sleep(delayMs / 1000)",
                        "    Path(marker).write_text(artifactNamespace, encoding='utf-8')",
                        "    return StartResult(capture=Capture(",
                        "        captureId=21, namespace=artifactNamespace",
                        "    ))",
                        "if __name__ == '__main__':",
                        "    server.run(transport='stdio')",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            transport = StdioMcpToolTransport(
                command=sys.executable,
                args=[str(server)],
                cwd=root,
            )
            first_marker = root / "first.txt"
            queued_marker = root / "queued.txt"
            restart_marker = root / "restart.txt"

            async def exercise() -> dict[str, Any]:
                first = asyncio.create_task(
                    transport.call_tool(
                        "start_stream_capture",
                        {
                            "artifactNamespace": "first",
                            "marker": str(first_marker),
                            "delayMs": 2_000,
                        },
                        Deadline(5_000),
                    )
                )
                await asyncio.sleep(0.1)
                with self.assertRaises(AdapterError):
                    await transport.call_tool(
                        "start_stream_capture",
                        {
                            "artifactNamespace": "queued",
                            "marker": str(queued_marker),
                            "delayMs": 0,
                        },
                        Deadline(150),
                    )
                await asyncio.gather(first, return_exceptions=True)
                await asyncio.sleep(0.3)
                result = await transport.call_tool(
                    "start_stream_capture",
                    {
                        "artifactNamespace": "restart",
                        "marker": str(restart_marker),
                        "delayMs": 0,
                    },
                    Deadline(5_000),
                )
                await transport.close()
                return result

            result = asyncio.run(exercise())
            self.assertFalse(queued_marker.exists())
            self.assertTrue(restart_marker.is_file())
            self.assertEqual(result["capture"]["namespace"], "restart")

    def test_playwright_output_is_streamed_into_a_bounded_buffer(self) -> None:
        async def exercise() -> Any:
            runner = SubprocessCommandRunner(max_output_bytes=200)
            return await runner.run(
                [
                    sys.executable,
                    "-c",
                    ("import sys;sys.stdout.write('x'*10000);sys.stderr.write('y'*10000)"),
                ],
                deadline=Deadline(5_000),
            )

        result = asyncio.run(exercise())
        self.assertTrue(result.truncated)
        self.assertLessEqual(
            len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")),
            200,
        )

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_playwright_runner_timeout_terminates_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_pid_file = root / "child.pid"
            parent = root / "parent.py"
            parent.write_text(
                "\n".join(
                    [
                        "import subprocess, sys, time",
                        "from pathlib import Path",
                        "child = subprocess.Popen([",
                        "    sys.executable, '-c', 'import time; time.sleep(30)'",
                        "])",
                        f"pid_file = Path({str(child_pid_file)!r})",
                        "pid_file.write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(30)",
                    ]
                ),
                encoding="utf-8",
            )

            async def exercise() -> None:
                runner = SubprocessCommandRunner()
                with self.assertRaises(AdapterError):
                    await runner.run(
                        [sys.executable, str(parent)],
                        deadline=Deadline(500),
                        cwd=root,
                    )

            asyncio.run(exercise())
            child_pid = child_pid_file.read_text(encoding="utf-8").strip()
            listing = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    f"PID eq {child_pid}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotIn(f'"{child_pid}"', listing.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_playwright_runner_cancellation_terminates_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_pid_file = root / "child.pid"
            marker = root / "child-finished.txt"
            parent = root / "parent.py"
            child_code = (
                "import time; from pathlib import Path; "
                f"time.sleep(2); Path({str(marker)!r}).write_text('finished')"
            )
            parent.write_text(
                "\n".join(
                    [
                        "import subprocess, sys, time",
                        "from pathlib import Path",
                        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])",
                        f"pid_file = Path({str(child_pid_file)!r})",
                        "pid_file.write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(30)",
                    ]
                ),
                encoding="utf-8",
            )

            async def exercise() -> None:
                runner = SubprocessCommandRunner()
                task = asyncio.create_task(
                    runner.run(
                        [sys.executable, str(parent)],
                        deadline=Deadline(30_000),
                        cwd=root,
                    )
                )
                for _ in range(100):
                    if child_pid_file.is_file():
                        break
                    await asyncio.sleep(0.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(exercise())
            child_pid = child_pid_file.read_text(encoding="utf-8").strip()
            time.sleep(2.2)
            self.assertFalse(marker.exists())
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotIn(f'"{child_pid}"', listing.stdout)

    def test_canceling_active_side_effect_mcp_call_restarts_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = root / "cancel_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "import asyncio",
                        "from pathlib import Path",
                        "from mcp.server.fastmcp import FastMCP",
                        "from pydantic import BaseModel",
                        "server = FastMCP('cancel-test')",
                        "class Capture(BaseModel):",
                        "    captureId: int",
                        "class Result(BaseModel):",
                        "    capture: Capture",
                        "@server.tool(structured_output=True)",
                        "async def start_stream_capture(marker: str, delayMs: int = 0) -> Result:",
                        "    await asyncio.sleep(delayMs / 1000)",
                        "    Path(marker).write_text('started', encoding='utf-8')",
                        "    return Result(capture=Capture(captureId=31))",
                        "if __name__ == '__main__':",
                        "    server.run(transport='stdio')",
                    ]
                ),
                encoding="utf-8",
            )
            canceled_marker = root / "canceled.txt"
            restarted_marker = root / "restarted.txt"
            transport = StdioMcpToolTransport(
                command=sys.executable,
                args=[str(server)],
                cwd=root,
            )

            async def exercise() -> dict[str, Any]:
                active = asyncio.create_task(
                    transport.call_tool(
                        "start_stream_capture",
                        {"marker": str(canceled_marker), "delayMs": 2_000},
                        Deadline(10_000),
                    )
                )
                await asyncio.sleep(0.3)
                active.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await active
                await asyncio.sleep(2.1)
                result = await transport.call_tool(
                    "start_stream_capture",
                    {"marker": str(restarted_marker), "delayMs": 0},
                    Deadline(5_000),
                )
                await transport.close()
                return result

            result = asyncio.run(exercise())
            self.assertFalse(canceled_marker.exists())
            self.assertTrue(restarted_marker.is_file())
            self.assertEqual(result["capture"]["captureId"], 31)

    def test_read_only_mcp_transport_crash_restarts_on_next_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "crashed-once.txt"
            server = root / "crash_once_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "import os",
                        "from pathlib import Path",
                        "from mcp.server.fastmcp import FastMCP",
                        "from pydantic import BaseModel",
                        "server = FastMCP('crash-once-test')",
                        "class Capture(BaseModel):",
                        "    captureId: int",
                        "    captureUuid: str",
                        "class Result(BaseModel):",
                        "    capture: Capture",
                        "@server.tool(structured_output=True)",
                        "async def get_stream_status(marker: str) -> Result:",
                        "    path = Path(marker)",
                        "    if not path.exists():",
                        "        path.write_text('crashed', encoding='utf-8')",
                        "        os._exit(17)",
                        "    return Result(capture=Capture(",
                        "        captureId=42,",
                        "        captureUuid='44444444-4444-4444-8444-444444444444',",
                        "    ))",
                        "if __name__ == '__main__':",
                        "    server.run(transport='stdio')",
                    ]
                ),
                encoding="utf-8",
            )
            transport = StdioMcpToolTransport(
                command=sys.executable,
                args=[str(server)],
                cwd=root,
            )

            async def exercise() -> tuple[int, dict[str, Any]]:
                first_generation = 0
                try:
                    await transport.call_tool(
                        "get_stream_status",
                        {"marker": str(marker)},
                        Deadline(5_000),
                    )
                except BaseException as exc:
                    self.assertNotIsInstance(
                        exc,
                        (KeyboardInterrupt, SystemExit, asyncio.CancelledError),
                    )
                else:
                    self.fail("The first MCP process should have exited")
                self.assertTrue(marker.is_file())
                self.assertEqual(marker.read_text(encoding="utf-8"), "crashed")
                first_generation = transport.generation
                result = await transport.call_tool(
                    "get_stream_status",
                    {"marker": str(marker)},
                    Deadline(5_000),
                )
                second_generation = transport.generation
                await transport.close()
                self.assertGreater(second_generation, first_generation)
                return second_generation, result

            generation, result = asyncio.run(exercise())
            self.assertGreaterEqual(generation, 2)
            self.assertEqual(result["capture"]["captureId"], 42)

    def test_playwright_locator_rendering_uses_supported_cli_locators(self) -> None:
        adapter = PlaywrightCliAdapter()
        self.assertEqual(adapter.render_locator(Locator(ref="e12")), "e12")
        self.assertEqual(adapter.render_locator(Locator(css="#send")), "#send")
        self.assertEqual(
            adapter.render_locator(Locator(role="button", name="Send")),
            'getByRole("button", { name: "Send" })',
        )
        self.assertEqual(
            adapter.render_locator(Locator(placeholder="Input")),
            'getByPlaceholder("Input")',
        )

    def test_playwright_attach_argv_matches_stage0_verified_format(self) -> None:
        self.assertEqual(
            build_playwright_attach_args(
                "http://127.0.0.1:9222",
                "session_one",
            ),
            [
                "attach",
                "--cdp",
                "http://127.0.0.1:9222",
                "--session",
                "session_one",
            ],
        )
