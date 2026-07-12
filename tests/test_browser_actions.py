from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.browser_adapters import (
    AlignmentResult,
    JsReverseMcpAdapter,
    PageState,
    StdioMcpToolTransport,
    StreamWaitResult,
)
from skill_temple.browser_models import FlowStep, Locator, RequestMatcher, WaitCondition
from skill_temple.browser_service import (
    BrowserActionService,
    Deadline,
    WorkspaceStore,
    build_browser_service_from_environment,
)


class FakePlaywright:
    def __init__(self, events: list[str], *, fail_step: str | None = None) -> None:
        self.events = events
        self.fail_step = fail_step
        self.page = PageState(url="https://example.test/app", title="Example")

    async def open_session(
        self,
        session_ref: str,
        browser_endpoint: str,
        start_url: str | None,
        deadline: Deadline,
    ) -> PageState:
        self.events.append("playwright.open")
        if start_url:
            self.page = PageState(url=start_url, title="Example")
        return self.page

    async def current_page(self, session_ref: str, deadline: Deadline) -> PageState:
        self.events.append("playwright.current_page")
        return self.page

    async def select_page(
        self,
        session_ref: str,
        page_index: int,
        deadline: Deadline,
    ) -> PageState:
        self.events.append(f"playwright.select_page:{page_index}")
        self.page = PageState(
            url=self.page.url,
            title=self.page.title,
            page_index=page_index,
        )
        return self.page

    async def execute_step(
        self,
        session_ref: str,
        step: FlowStep,
        experiment_dir: Path,
        deadline: Deadline,
    ) -> dict[str, Any]:
        self.events.append(f"playwright.step:{step.step_id}")
        if step.step_id == self.fail_step:
            raise RuntimeError("synthetic step failure")
        if step.action == "navigate" and step.value:
            self.page = PageState(url=step.value, title="Example")
        snapshot = experiment_dir / "playwright" / f"{step.step_id}.yaml"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("snapshot\n", encoding="utf-8")
        return {"snapshot_ref": snapshot.as_posix()}

    async def wait_for_page_condition(
        self,
        session_ref: str,
        condition: WaitCondition,
        deadline: Deadline,
    ) -> dict[str, Any]:
        self.events.append(f"playwright.wait:{condition.type}")
        return {"condition_met": True, "type": condition.type}

    async def start_trace(self, session_ref: str, deadline: Deadline) -> None:
        self.events.append("playwright.trace_start")

    async def stop_trace(
        self, session_ref: str, experiment_dir: Path, deadline: Deadline
    ) -> list[str]:
        self.events.append("playwright.trace_stop")
        trace = experiment_dir / "playwright" / "trace.zip"
        trace.write_bytes(b"trace")
        return [trace.as_posix()]

    async def capture_screenshot(
        self,
        session_ref: str,
        experiment_dir: Path,
        name: str,
        deadline: Deadline,
    ) -> str:
        self.events.append(f"playwright.screenshot:{name}")
        screenshot = experiment_dir / "playwright" / "screenshots" / f"{name}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"png")
        return screenshot.as_posix()

    async def close_session(self, session_ref: str, deadline: Deadline) -> None:
        self.events.append("playwright.close")


class FakeJsReverse:
    def __init__(
        self,
        events: list[str],
        workspace_root: Path,
        *,
        alignment_status: str = "aligned",
        include_supporting_failure: bool = True,
        primary_status: str = "finished",
    ) -> None:
        self.events = events
        self.workspace_root = workspace_root
        self.alignment_status = alignment_status
        self.include_supporting_failure = include_supporting_failure
        self.primary_status = primary_status
        self.start_arguments: dict[str, Any] | None = None
        self.capture_id = 1
        self.experiment_id = ""
        self.status_payload: dict[str, Any] = {}

    async def align_page(self, page: PageState, deadline: Deadline) -> AlignmentResult:
        self.events.append("js.align")
        if self.alignment_status != "aligned":
            return AlignmentResult(
                status="not_aligned",
                playwright_page=page,
                warnings=["synthetic alignment failure"],
            )
        return AlignmentResult(
            status="aligned",
            playwright_page=page,
            js_reverse_page_index=0,
            js_reverse_page_url=page.url,
        )

    def _write_artifacts(self, experiment_id: str) -> dict[str, dict[str, Any]]:
        request_dir = (
            self.workspace_root
            / "experiments"
            / experiment_id
            / "js-reverse"
            / "capture-fake"
            / "request-0001"
        )
        request_dir.mkdir(parents=True, exist_ok=True)
        events_path = request_dir / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "index": 0,
                    "eventName": "message",
                    "data": "[DONE]",
                    "defaultDoneMarker": True,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        full_headers = request_dir / "request-headers.json"
        full_headers.write_text(
            json.dumps({"authorization": "Bearer secret"}), encoding="utf-8"
        )
        redacted_headers = request_dir / "request-headers.redacted.json"
        redacted_headers.write_text(
            json.dumps({"authorization": "[REDACTED]"}), encoding="utf-8"
        )
        metadata_path = request_dir / "metadata.json"
        metadata_path.write_text(json.dumps({"status": "finished"}), encoding="utf-8")

        prefix = f"experiments/{experiment_id}/js-reverse/capture-fake/request-0001"
        return {
            "events": {
                "artifactId": f"art_{experiment_id}_events",
                "kind": "events",
                "relativePath": f"{prefix}/events.jsonl",
                "sensitivity": "private",
            },
            "headers": {
                "artifactId": f"art_{experiment_id}_headers",
                "kind": "request_headers",
                "relativePath": f"{prefix}/request-headers.json",
                "sensitivity": "credential",
                "containsCredentials": True,
                "redactedArtifactId": f"art_{experiment_id}_headers_redacted",
            },
            "redacted": {
                "artifactId": f"art_{experiment_id}_headers_redacted",
                "kind": "request_headers_redacted",
                "relativePath": f"{prefix}/request-headers.redacted.json",
                "sensitivity": "public",
                "containsCredentials": False,
            },
            "metadata": {
                "artifactId": f"art_{experiment_id}_request_metadata",
                "kind": "request_metadata",
                "relativePath": f"{prefix}/metadata.json",
                "sensitivity": "private",
            },
        }

    async def start_stream_capture(
        self,
        *,
        experiment_id: str,
        matcher: RequestMatcher,
        include_in_flight: bool,
        deadline: Deadline,
    ) -> dict[str, Any]:
        self.events.append("js.start")
        self.experiment_id = experiment_id
        self.start_arguments = {
            "experiment_id": experiment_id,
            "matcher": matcher.model_dump(exclude_none=True),
            "include_in_flight": include_in_flight,
        }
        artifacts = self._write_artifacts(experiment_id)
        primary = {
            "cdpRequestId": "primary-cdp",
            "persistentRequestId": f"req_{experiment_id}_primary",
            "url": "https://example.test/conversation",
            "method": "POST",
            "resourceType": "fetch",
            "status": self.primary_status,
            "terminalReason": (
                "network_canceled" if self.primary_status == "canceled" else "completed"
            ),
            "integrityStatus": "complete",
            "rawCaptureIntegrity": "complete",
            "semanticParseIntegrity": "complete",
            "requestSnapshotIntegrity": "complete",
            "artifactIntegrity": "complete",
            "responseObserved": True,
            "defaultDoneMarkerObserved": True,
            "rawEventCount": 1,
            "coreArtifacts": list(artifacts.values()),
        }
        requests = [primary]
        if self.include_supporting_failure:
            requests.append(
                {
                    "cdpRequestId": "telemetry-cdp",
                    "persistentRequestId": f"req_{experiment_id}_telemetry",
                    "url": "https://example.test/telemetry",
                    "method": "POST",
                    "resourceType": "fetch",
                    "status": "failed",
                    "integrityStatus": "failed",
                }
            )
        self.status_payload = {
            "capture": {
                "captureId": self.capture_id,
                "captureUuid": "11111111-1111-4111-8111-111111111111",
                "status": "capturing",
                "integrityStatus": "failed",
                "collectorIntegrity": "failed",
                "captureScope": "page-target-only",
                "workerCoverage": False,
                "version": 2,
            },
            "requests": requests,
        }
        return {"capture": self.status_payload["capture"]}

    async def get_stream_status(
        self, capture_id: int, deadline: Deadline
    ) -> dict[str, Any]:
        self.events.append("js.status")
        if self.primary_status == "canceled":
            self.status_payload["requests"][0]["endedWallTimeMs"] = int(
                time.time() * 1000
            )
        return self.status_payload

    async def list_network_requests(
        self, matcher: RequestMatcher, deadline: Deadline
    ) -> dict[str, Any]:
        self.events.append("js.network")
        return {"requests": self.status_payload.get("requests", [])}

    async def wait_for_stream_condition(
        self,
        *,
        capture_id: int,
        request_matcher: RequestMatcher,
        condition: WaitCondition,
        since_version: int,
        deadline: Deadline,
    ) -> StreamWaitResult:
        self.events.append("js.wait")
        return StreamWaitResult(
            condition_met=True,
            capture_id=capture_id,
            capture_version=2,
            matched_request_ids=["primary-cdp"],
            terminal_status=self.primary_status,
            matched_event={"data": "[DONE]"},
            status_payload=self.status_payload,
        )

    async def stop_stream_capture(
        self, capture_id: int, deadline: Deadline
    ) -> dict[str, Any]:
        self.events.append("js.stop")
        self.status_payload["capture"]["status"] = "stopped"
        artifacts = self.status_payload["requests"][0]["coreArtifacts"]
        metadata = next(item for item in artifacts if item["kind"] == "request_metadata")
        return {
            "capture": self.status_payload["capture"],
            "captureMetadataArtifact": {
                "artifactId": f"art_{self.experiment_id}_capture",
                "kind": "capture_metadata",
                "relativePath": f"experiments/{self.experiment_id}/manifest.json",
            },
            "requestMetadataArtifacts": [metadata],
        }

    async def close(self) -> None:
        self.events.append("js.close")


class BrowserActionTests(unittest.TestCase):
    def make_client(
        self,
        root: Path,
        *,
        fail_step: str | None = None,
        alignment_status: str = "aligned",
        include_supporting_failure: bool = True,
        primary_status: str = "finished",
    ) -> tuple[TestClient, list[str], FakeJsReverse]:
        events: list[str] = []
        workspace = WorkspaceStore(root)
        js = FakeJsReverse(
            events,
            workspace.root,
            alignment_status=alignment_status,
            include_supporting_failure=include_supporting_failure,
            primary_status=primary_status,
        )
        service = BrowserActionService(
            playwright=FakePlaywright(events, fail_step=fail_step),
            js_reverse=js,
            workspace=workspace,
            default_browser_endpoint="http://127.0.0.1:9222",
        )
        return TestClient(create_app(browser_service=service)), events, js

    @staticmethod
    def open_session(client: TestClient) -> None:
        response = client.post(
            "/v1/browser/run",
            json={
                "operation": "open_session",
                "payload": {
                    "session_id": "session_one",
                    "target": {"start_url": "https://example.test/app"},
                },
            },
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def capture_request(*, include_in_flight: bool = False) -> dict[str, Any]:
        return {
            "operation": "capture_flow",
            "payload": {
                "session_id": "session_one",
                "objective": "capture one conversation stream",
                "target": {"expected_url_contains": "/app"},
                "primary_request": {
                    "url_contains": "/conversation",
                    "method": "POST",
                    "resource_types": ["fetch"],
                    "expected_min_matches": 1,
                    "expected_max_matches": 1,
                    "allow_supporting_failures": True,
                    "include_in_flight": include_in_flight,
                },
                "flow": [
                    {
                        "step_id": "send_message",
                        "action": "fill",
                        "locator": {"placeholder": "Message"},
                        "value": "hello",
                    },
                    {
                        "step_id": "click_send",
                        "action": "click",
                        "locator": {"role": "button", "name": "Send"},
                    },
                ],
                "wait_for": {
                    "type": "default_done_marker",
                    "request_matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                },
                "deadline_ms": 10_000,
            },
        }

    def test_openapi_has_two_browser_actions_and_discriminated_unions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            schema = client.get("/openapi.json").json()
        run = schema["paths"]["/v1/browser/run"]["post"]
        inspect = schema["paths"]["/v1/browser/inspect"]["post"]
        self.assertIs(run["x-openai-isConsequential"], True)
        self.assertIs(inspect["x-openai-isConsequential"], False)
        run_schema = run["requestBody"]["content"]["application/json"]["schema"]
        inspect_schema = inspect["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("oneOf", run_schema)
        self.assertIn("discriminator", run_schema)
        self.assertIn("oneOf", inspect_schema)
        self.assertIn("discriminator", inspect_schema)

    def test_atomic_capture_order_manifest_and_primary_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, js = self.make_client(root)
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["status"], "completed")
            experiment = body["result"]["experiment"]
            self.assertEqual(experiment["primary_request_integrity"], "complete")
            self.assertEqual(experiment["objective_integrity"], "complete")
            self.assertEqual(experiment["collector_integrity"], "failed")
            self.assertTrue(experiment["capture_health"]["collector_stopped"])
            self.assertFalse(experiment["capture_health"]["worker_coverage"])
            artifact_kinds = {item["kind"] for item in experiment["artifacts"]}
            self.assertIn("playwright_screenshot", artifact_kinds)
            self.assertIn("playwright_trace", artifact_kinds)
            self.assertTrue(experiment["network_summary"]["requests"])
            self.assertLess(events.index("js.start"), events.index("playwright.step:send_message"))
            self.assertLess(events.index("playwright.step:click_send"), events.index("js.wait"))
            self.assertLess(events.index("js.wait"), events.index("js.stop"))
            manifest = root / body["result"]["manifest_relative_path"]
            self.assertTrue(manifest.is_file())
            self.assertEqual(js.start_arguments["include_in_flight"], False)

    def test_failure_still_stops_collector_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, _ = self.make_client(root, fail_step="click_send")
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["status"], "failed")
            self.assertIn("js.stop", events)
            manifest = root / body["result"]["manifest_relative_path"]
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertTrue(saved["errors"])

    def test_alignment_failure_prevents_stream_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, events, _ = self.make_client(
                Path(temp_dir), alignment_status="not_aligned"
            )
            with client:
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "open_session",
                        "payload": {"session_id": "session_one"},
                    },
                )
            self.assertEqual(response.status_code, 409)
            self.assertNotIn("js.start", events)
            self.assertIn("playwright.close", events)

    def test_inspect_defaults_to_redacted_credential_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                capture = client.post("/v1/browser/run", json=self.capture_request()).json()
                experiment_id = capture["experiment_id"]
                artifacts = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "list_artifacts",
                        "payload": {"experiment_id": experiment_id},
                    },
                ).json()["result"]["artifacts"]
                credential = next(
                    item for item in artifacts if item.get("sensitivity") == "credential"
                )
                redacted = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "read_artifact",
                        "payload": {
                            "experiment_id": experiment_id,
                            "artifact_id": credential["artifactId"],
                        },
                    },
                )
                full = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "read_artifact",
                        "payload": {
                            "experiment_id": experiment_id,
                            "artifact_id": credential["artifactId"],
                            "credential_mode": "full",
                        },
                    },
                )
            self.assertEqual(redacted.status_code, 200, redacted.text)
            self.assertIn("[REDACTED]", redacted.json()["result"]["content"])
            self.assertNotIn("Bearer secret", redacted.json()["result"]["content"])
            self.assertIn("Bearer secret", full.json()["result"]["content"])

    def test_include_in_flight_is_forwarded_and_close_session_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, js = self.make_client(root)
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run",
                    json=self.capture_request(include_in_flight=True),
                )
                close = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "close_session",
                        "payload": {"session_id": "session_one"},
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(js.start_arguments["include_in_flight"])
            self.assertEqual(close.status_code, 200)
            self.assertIn("playwright.close", events)
            session = json.loads(
                (root / "sessions" / "session_one.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session["status"], "closed")

    def test_baseline_payload_uses_defaults_without_requiring_a_primary_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(
                Path(temp_dir), include_supporting_failure=False
            )
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "capture_baseline",
                        "payload": {"session_id": "session_one"},
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            experiment = response.json()["result"]["experiment"]
            self.assertEqual(
                experiment["primary_request_matcher"]["expected_min_matches"], 0
            )
            self.assertEqual(experiment["steps"], [])

    def test_supporting_failure_can_be_made_objective_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            request["payload"]["primary_request"]["allow_supporting_failures"] = False
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            experiment = response.json()["result"]["experiment"]
            self.assertEqual(experiment["primary_request_integrity"], "complete")
            self.assertEqual(experiment["collector_integrity"], "failed")
            self.assertEqual(experiment["objective_integrity"], "failed")

    def test_stop_intent_correlates_only_the_primary_network_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(
                Path(temp_dir),
                include_supporting_failure=False,
                primary_status="canceled",
            )
            request = self.capture_request()
            request["payload"]["flow"] = [
                {
                    "step_id": "stop_generation",
                    "action": "click",
                    "locator": {"role": "button", "name": "Stop"},
                    "intent": "stop_generation",
                }
            ]
            request["payload"]["wait_for"] = {
                "type": "network_canceled",
                "request_matcher": {
                    "url_contains": "/conversation",
                    "method": "POST",
                },
            }
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            experiment = response.json()["result"]["experiment"]
            classification = experiment["cancellation_classifications"][0]
            self.assertEqual(classification["classification"], "expected_user_cancel")
            self.assertTrue(classification["within_stop_window"])
            self.assertEqual(
                experiment["primary_requests"][0][
                    "experimentCancellationClassification"
                ],
                "expected_user_cancel",
            )

    def test_environment_builder_binds_mcp_to_workspace_and_same_cdp_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "WEB_REV_WORKSPACE_DIR": temp_dir,
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

    def test_configured_private_mcp_rejects_a_different_playwright_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            events: list[str] = []
            workspace = WorkspaceStore(Path(temp_dir))
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, workspace.root),
                workspace=workspace,
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
                    return {
                        "capture": {"captureId": 7, "version": 2},
                        "requests": [
                            {
                                "cdpRequestId": "req-7",
                                "url": "https://example.test/conversation",
                                "method": "POST",
                                "resourceType": "fetch",
                                "status": "finished",
                                "defaultDoneMarkerObserved": True,
                            }
                        ],
                    }
                if name == "stop_stream_capture":
                    return {"capture": {"captureId": 7, "status": "stopped"}}
                return {}

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            transport = FakeTransport()
            adapter = JsReverseMcpAdapter(
                transport,
                workspace_root=Path(temp_dir),
            )

            async def exercise() -> StreamWaitResult:
                deadline = Deadline(5_000)
                started = await adapter.start_stream_capture(
                    experiment_id="exp_private",
                    matcher=RequestMatcher(
                        url_contains="/conversation",
                        method="POST",
                        resource_types=["fetch"],
                    ),
                    include_in_flight=False,
                    deadline=deadline,
                )
                self.assertEqual(started["capture"]["captureId"], 7)
                waited = await adapter.wait_for_stream_condition(
                    capture_id=7,
                    request_matcher=RequestMatcher(
                        url_contains="/conversation", method="POST"
                    ),
                    condition=WaitCondition(
                        type="default_done_marker",
                        request_matcher=RequestMatcher(
                            url_contains="/conversation", method="POST"
                        ),
                    ),
                    since_version=1,
                    deadline=deadline,
                )
                await adapter.stop_stream_capture(7, deadline)
                return waited

            waited = asyncio.run(exercise())
            self.assertTrue(waited.condition_met)
            self.assertEqual(
                [name for name, _ in transport.calls],
                ["start_stream_capture", "get_stream_status", "stop_stream_capture"],
            )
            start_args = transport.calls[0][1]
            self.assertEqual(start_args["artifactNamespace"], "exp_private")
            self.assertFalse(start_args["includeInFlight"])

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

    def test_strict_flow_schema_rejects_missing_locator_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                request = self.capture_request()
                request["payload"]["flow"] = [
                    {"step_id": "bad", "action": "click", "unknown": True}
                ]
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 422)

    def test_playwright_locator_rendering_uses_supported_cli_locators(self) -> None:
        from skill_temple.browser_adapters import PlaywrightCliAdapter

        adapter = PlaywrightCliAdapter()
        self.assertEqual(adapter.render_locator(Locator(ref="e12")), "e12")
        self.assertEqual(adapter.render_locator(Locator(css="#send")), "#send")
        self.assertEqual(
            adapter.render_locator(Locator(role="button", name="Send")),
            'getByRole("button", { name: "Send" })',
        )
        self.assertEqual(
            adapter.render_locator(Locator(placeholder="Message")),
            'getByPlaceholder("Message")',
        )


if __name__ == "__main__":
    unittest.main()
