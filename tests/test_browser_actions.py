from __future__ import annotations

import asyncio
import json
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
from skill_temple.browser_adapters import (
    AdapterError,
    AlignmentResult,
    JsReverseMcpAdapter,
    PageState,
    StdioMcpToolTransport,
    StreamCheckpoint,
    StreamWaitResult,
    SubprocessCommandRunner,
    build_playwright_attach_args,
)
from skill_temple.browser_models import (
    CaptureFlowRequest,
    ExactDataPredicate,
    FlowStep,
    Locator,
    OpenSessionRequest,
    RequestMatcher,
    WaitCondition,
)
from skill_temple.browser_service import (
    BrowserActionService,
    Deadline,
    ExperimentStore,
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
        self,
        session_ref: str,
        experiment_dir: Path,
        deadline: Deadline,
        *,
        collect_files: bool = True,
    ) -> list[str]:
        self.events.append("playwright.trace_stop")
        if not collect_files:
            return []
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
        evidence_root: Path,
        *,
        alignment_status: str = "aligned",
        include_supporting_failure: bool = True,
        primary_status: str = "finished",
        raw_capture_integrity: str = "complete",
        semantic_parse_integrity: str = "complete",
        request_snapshot_integrity: str = "complete",
        artifact_integrity: str = "complete",
        fail_stop: bool = False,
        post_alignment_status: str | None = None,
    ) -> None:
        self.events = events
        self.evidence_root = evidence_root
        self.alignment_status = alignment_status
        self.include_supporting_failure = include_supporting_failure
        self.primary_status = primary_status
        self.raw_capture_integrity = raw_capture_integrity
        self.semantic_parse_integrity = semantic_parse_integrity
        self.request_snapshot_integrity = request_snapshot_integrity
        self.artifact_integrity = artifact_integrity
        self.fail_stop = fail_stop
        self.post_alignment_status = post_alignment_status
        self.start_arguments: dict[str, Any] | None = None
        self.capture_id = 1
        self.experiment_id = ""
        self.status_payload: dict[str, Any] = {}
        self.aligned_page_ids: list[str | None] = []
        self.alignment_calls = 0

    async def align_page(
        self,
        page: PageState,
        deadline: Deadline,
        page_id: str | None = None,
    ) -> AlignmentResult:
        self.events.append("js.align")
        self.aligned_page_ids.append(page_id)
        self.alignment_calls += 1
        effective_status = (
            self.post_alignment_status
            if self.post_alignment_status is not None and self.alignment_calls >= 3
            else self.alignment_status
        )
        if effective_status != "aligned":
            return AlignmentResult(
                status="not_aligned",
                playwright_page=page,
                warnings=["synthetic alignment failure"],
            )
        return AlignmentResult(
            status="aligned",
            playwright_page=page,
            js_reverse_page_index=0,
            js_reverse_page_id=page_id or "page_fake",
            js_reverse_page_url=page.url,
        )

    def _write_artifacts(self, experiment_id: str) -> dict[str, dict[str, Any]]:
        request_dir = (
            self.evidence_root
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
            "rawCaptureIntegrity": self.raw_capture_integrity,
            "semanticParseIntegrity": self.semantic_parse_integrity,
            "requestSnapshotIntegrity": self.request_snapshot_integrity,
            "artifactIntegrity": self.artifact_integrity,
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
        self,
        capture_id: int,
        deadline: Deadline,
        *,
        request_id: str | None = None,
        event_predicate: Any | None = None,
        after_event_index: int = -1,
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
        checkpoint: StreamCheckpoint,
        deadline: Deadline,
    ) -> StreamWaitResult:
        self.events.append("js.wait")
        version = max(3, checkpoint.version + 1)
        matched_event = {
            "matched": True,
            "matchedEventIndex": version - 2,
            "matchedRequestId": "primary-cdp",
            "matchedSource": "raw-stream",
            "rawByteStart": (version - 2) * 16,
            "rawByteEnd": (version - 1) * 16,
        }
        return StreamWaitResult(
            condition_met=True,
            capture_id=capture_id,
            capture_version=version,
            matched_request_ids=["primary-cdp"],
            terminal_status=self.primary_status,
            matched_event=matched_event,
            event_indices={"primary-cdp": version - 2},
            status_payload=self.status_payload,
        )

    async def stop_stream_capture(
        self, capture_id: int, deadline: Deadline
    ) -> dict[str, Any]:
        self.events.append("js.stop")
        if self.fail_stop:
            raise RuntimeError("synthetic stop failure")
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
        raw_capture_integrity: str = "complete",
        semantic_parse_integrity: str = "complete",
        request_snapshot_integrity: str = "complete",
        artifact_integrity: str = "complete",
        fail_stop: bool = False,
        post_alignment_status: str | None = None,
    ) -> tuple[TestClient, list[str], FakeJsReverse]:
        events: list[str] = []
        experiments = ExperimentStore(root)
        js = FakeJsReverse(
            events,
            experiments.root,
            alignment_status=alignment_status,
            include_supporting_failure=include_supporting_failure,
            primary_status=primary_status,
            raw_capture_integrity=raw_capture_integrity,
            semantic_parse_integrity=semantic_parse_integrity,
            request_snapshot_integrity=request_snapshot_integrity,
            artifact_integrity=artifact_integrity,
            fail_stop=fail_stop,
            post_alignment_status=post_alignment_status,
        )
        service = BrowserActionService(
            playwright=FakePlaywright(events, fail_step=fail_step),
            js_reverse=js,
            experiments=experiments,
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
                "execution_mode": "sync",
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
            summary = body["result"]["experiment"]
            manifest = root / body["result"]["manifest_relative_path"]
            experiment = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(summary["objective_integrity"], "complete")
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
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root, include_supporting_failure=False
            )
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "capture_baseline",
                        "payload": {
                            "session_id": "session_one",
                            "execution_mode": "sync",
                        },
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            experiment = json.loads(
                (root / body["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                experiment["primary_request_matcher"]["expected_min_matches"], 0
            )
            self.assertEqual(experiment["steps"], [])
            self.assertEqual(response.json()["status"], "completed")
            self.assertEqual(experiment["objective_integrity"], "complete")

    def test_supporting_failure_can_be_made_objective_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            request["payload"]["primary_request"]["allow_supporting_failures"] = False
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            experiment = json.loads(
                (
                    Path(temp_dir)
                    / body["result"]["manifest_relative_path"]
                ).read_text(encoding="utf-8")
            )
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
                    "step_id": "wait_stream_started",
                    "action": "wait",
                    "condition": {
                        "type": "first_event",
                        "request_matcher": {
                            "url_contains": "/conversation",
                            "method": "POST",
                        },
                    },
                },
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
            body = response.json()
            experiment = json.loads(
                (
                    Path(temp_dir)
                    / body["result"]["manifest_relative_path"]
                ).read_text(encoding="utf-8")
            )
            classification = experiment["cancellation_classifications"][0]
            self.assertEqual(classification["classification"], "expected_user_cancel")
            self.assertTrue(classification["within_stop_window"])
            self.assertEqual(
                experiment["primary_requests"][0][
                    "experimentCancellationClassification"
                ],
                "expected_user_cancel",
            )
            self.assertIsNotNone(classification["stream_before_stop"])

    def test_stop_intent_requires_stream_start_observation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
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
                "request_matcher": {"url_contains": "/conversation"},
            }
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 422)
            self.assertIn("requires an earlier first_event", response.text)

    def test_job_mode_returns_running_then_completes_via_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            request["payload"].pop("execution_mode")
            request["payload"]["job_timeout_ms"] = 30_000
            with client:
                self.open_session(client)
                started = client.post("/v1/browser/run", json=request)
                self.assertEqual(started.status_code, 200, started.text)
                self.assertEqual(started.json()["status"], "running")
                experiment_id = started.json()["experiment_id"]
                final: dict[str, Any] | None = None
                for _ in range(100):
                    inspected = client.post(
                        "/v1/browser/inspect",
                        json={
                            "operation": "get_experiment",
                            "payload": {"experiment_id": experiment_id},
                        },
                    )
                    self.assertEqual(inspected.status_code, 200, inspected.text)
                    if inspected.json()["status"] != "running":
                        final = inspected.json()
                        break
                    time.sleep(0.02)
            self.assertIsNotNone(final)
            self.assertEqual(final["status"], "completed")
            manifest = Path(temp_dir) / final["result"]["manifest_relative_path"]
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8"))["execution_mode"],
                "job",
            )

    def test_experiment_store_recovers_running_manifest_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiment_dir = root / "experiments" / "exp_crashed"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp_crashed",
                        "session_id": "session_one",
                        "status": "running",
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )
            experiments = ExperimentStore(root)
            recovered = experiments.load_manifest("exp_crashed")
            self.assertEqual(recovered["status"], "interrupted")
            self.assertTrue(recovered["errors"])

    def test_session_reuses_stable_js_reverse_page_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, js = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run", json=self.capture_request()
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(js.aligned_page_ids, [None, "page_fake", "page_fake"])

    def test_capture_without_page_index_reuses_session_selected_tab(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, events, _ = self.make_client(Path(temp_dir))
            with client:
                opened = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "open_session",
                        "payload": {
                            "session_id": "session_one",
                            "target": {"page_index": 2},
                        },
                    },
                )
                self.assertEqual(opened.status_code, 200, opened.text)
                response = client.post(
                    "/v1/browser/run", json=self.capture_request()
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(events.count("playwright.select_page:2"), 2)
            self.assertNotIn("playwright.select_page:0", events)

    def test_capture_rejects_implicit_target_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            request["payload"]["target"]["start_url"] = "https://example.test/new"
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 422)
            self.assertIn("explicit navigate flow step", response.text)

    def test_persisted_open_session_becomes_stale_after_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiments = ExperimentStore(root)
            experiments.save_session(
                {
                    "session_id": "session_old",
                    "status": "open",
                    "service_instance_id": "svc_old",
                    "playwright_session_ref": "session_old",
                    "playwright_page_index": 0,
                }
            )
            events: list[str] = []
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, root),
                experiments=experiments,
                default_browser_endpoint="http://127.0.0.1:9222",
            )
            client = TestClient(create_app(browser_service=service))
            with client:
                response = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_session",
                        "payload": {"session_id": "session_old"},
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["result"]["session"]["status"], "stale")

    def test_partial_raw_capture_produces_partial_objective(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                include_supporting_failure=False,
                raw_capture_integrity="partial",
            )
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run", json=self.capture_request()
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "partial")
            manifest = json.loads(
                (
                    root / response.json()["result"]["manifest_relative_path"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["objective_integrity"], "partial")
            self.assertEqual(
                manifest["primary_integrity_dimensions"]["raw_capture"],
                "partial",
            )

    def test_stop_failure_records_orphan_capture_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, _ = self.make_client(root, fail_stop=True)
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run", json=self.capture_request()
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "failed")
            self.assertIn("js.stop", events)
            manifest = json.loads(
                (
                    root / response.json()["result"]["manifest_relative_path"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["capture_health"]["orphan_capture_id"], 1)

    def test_post_stop_alignment_failure_prevents_user_cancel_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                include_supporting_failure=False,
                primary_status="canceled",
                post_alignment_status="not_aligned",
            )
            request = self.capture_request()
            request["payload"]["flow"] = [
                {
                    "step_id": "wait_stream_started",
                    "action": "wait",
                    "condition": {
                        "type": "first_event",
                        "request_matcher": {"url_contains": "/conversation"},
                    },
                },
                {
                    "step_id": "stop_generation",
                    "action": "click",
                    "locator": {"role": "button", "name": "Stop"},
                    "intent": "stop_generation",
                },
            ]
            request["payload"]["wait_for"] = {
                "type": "network_canceled",
                "request_matcher": {"url_contains": "/conversation"},
            }
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            manifest = json.loads(
                (
                    root / response.json()["result"]["manifest_relative_path"]
                ).read_text(encoding="utf-8")
            )
            classification = manifest["cancellation_classifications"][0]
            self.assertEqual(
                classification["classification"],
                "unclassified_network_cancel",
            )
            self.assertFalse(classification["page_remained_aligned"])

    def test_shared_browser_runtime_serializes_different_sessions(self) -> None:
        class SlowPlaywright(FakePlaywright):
            def __init__(self, events: list[str]) -> None:
                super().__init__(events)
                self.active_steps = 0
                self.max_active_steps = 0

            async def execute_step(
                self,
                session_ref: str,
                step: FlowStep,
                experiment_dir: Path,
                deadline: Deadline,
            ) -> dict[str, Any]:
                self.active_steps += 1
                self.max_active_steps = max(
                    self.max_active_steps,
                    self.active_steps,
                )
                try:
                    await asyncio.sleep(0.03)
                    return await super().execute_step(
                        session_ref,
                        step,
                        experiment_dir,
                        deadline,
                    )
                finally:
                    self.active_steps -= 1

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            playwright = SlowPlaywright(events)
            js = FakeJsReverse(
                events,
                root,
                include_supporting_failure=False,
            )
            service = BrowserActionService(
                playwright=playwright,
                js_reverse=js,
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def scenario() -> None:
                for session_id in ["session_a", "session_b"]:
                    await service.run(
                        OpenSessionRequest(
                            operation="open_session",
                            payload={"session_id": session_id},
                        )
                    )
                requests = [
                    CaptureFlowRequest(
                        operation="capture_flow",
                        payload={
                            "session_id": session_id,
                            "objective": f"capture {session_id}",
                            "primary_request": {
                                "url_contains": "/conversation",
                                "method": "POST",
                                "resource_types": ["fetch"],
                            },
                            "flow": [
                                {
                                    "step_id": f"click_{session_id}",
                                    "action": "click",
                                    "locator": {
                                        "role": "button",
                                        "name": "Send",
                                    },
                                }
                            ],
                            "wait_for": {
                                "type": "default_done_marker",
                                "request_matcher": {
                                    "url_contains": "/conversation",
                                },
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 15_000,
                        },
                    )
                    for session_id in ["session_a", "session_b"]
                ]
                await asyncio.gather(*(service.run(item) for item in requests))
                await service.close()

            asyncio.run(scenario())
            self.assertEqual(playwright.max_active_steps, 1)

    def test_task_cancellation_stops_capture_and_writes_interrupted_manifest(self) -> None:
        class BlockingPlaywright(FakePlaywright):
            def __init__(self, events: list[str]) -> None:
                super().__init__(events)
                self.started = asyncio.Event()

            async def execute_step(
                self,
                session_ref: str,
                step: FlowStep,
                experiment_dir: Path,
                deadline: Deadline,
            ) -> dict[str, Any]:
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            playwright = BlockingPlaywright(events)
            js = FakeJsReverse(
                events,
                root,
                include_supporting_failure=False,
            )
            service = BrowserActionService(
                playwright=playwright,
                js_reverse=js,
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def scenario() -> None:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "session_cancel"},
                    )
                )
                request = CaptureFlowRequest(
                    operation="capture_flow",
                    payload={
                        "session_id": "session_cancel",
                        "objective": "cancel during click",
                        "primary_request": {
                            "url_contains": "/conversation",
                            "method": "POST",
                            "resource_types": ["fetch"],
                        },
                        "flow": [
                            {
                                "step_id": "blocking_click",
                                "action": "click",
                                "locator": {
                                    "role": "button",
                                    "name": "Send",
                                },
                            }
                        ],
                        "execution_mode": "sync",
                        "deadline_ms": 15_000,
                    },
                )
                task = asyncio.create_task(service.run(request))
                await playwright.started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await service.close()

            asyncio.run(scenario())
            self.assertIn("js.stop", events)
            manifests = list((root / "experiments").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "interrupted")
            self.assertTrue(manifest["capture_health"]["collector_stopped"])

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
                                "url": "https://example.test/conversation",
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
                        url_contains="/conversation",
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
                    request_matcher=RequestMatcher(
                        url_contains="/conversation", method="POST"
                    ),
                    condition=WaitCondition(
                        type="event_predicate",
                        request_matcher=RequestMatcher(
                            url_contains="/conversation", method="POST"
                        ),
                        predicate=ExactDataPredicate(
                            type="exact_data",
                            value="[DONE]",
                        ),
                    ),
                    checkpoint=StreamCheckpoint(
                        version=1,
                        event_indices={"req-7": -1},
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
                {"type": "exact_data", "value": "[DONE]"},
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

    def test_strict_flow_and_predicate_unions_reject_cross_action_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                click_request = self.capture_request()
                click_request["payload"]["flow"] = [
                    {
                        "step_id": "bad_click",
                        "action": "click",
                        "locator": {"role": "button", "name": "Send"},
                        "value": "not-allowed",
                    }
                ]
                click_response = client.post(
                    "/v1/browser/run",
                    json=click_request,
                )
                fill_request = self.capture_request()
                fill_request["payload"]["flow"] = [
                    {
                        "step_id": "bad_fill",
                        "action": "fill",
                        "locator": {"placeholder": "Message"},
                        "value": "hello",
                        "intent": "stop_generation",
                    }
                ]
                fill_response = client.post(
                    "/v1/browser/run",
                    json=fill_request,
                )
                predicate_request = self.capture_request()
                predicate_request["payload"]["wait_for"] = {
                    "type": "event_predicate",
                    "request_matcher": {"url_contains": "/conversation"},
                    "predicate": {
                        "type": "exact_data",
                        "value": "[DONE]",
                        "path": "$.type",
                    },
                }
                predicate_response = client.post(
                    "/v1/browser/run",
                    json=predicate_request,
                )
            self.assertEqual(click_response.status_code, 422)
            self.assertEqual(fill_response.status_code, 422)
            self.assertEqual(predicate_response.status_code, 422)

    def test_semantic_only_event_satisfies_first_event_after_checkpoint(self) -> None:
        class SemanticTransport:
            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "get_stream_status":
                    raise AssertionError(name)
                return {
                    "capture": {"captureId": 7, "version": 2},
                    "requests": [
                        {
                            "cdpRequestId": "req-semantic",
                            "url": "https://example.test/conversation",
                            "method": "POST",
                            "resourceType": "eventsource",
                            "status": "streaming",
                            "rawEventCount": 0,
                            "semanticEventCount": 1,
                        }
                    ],
                }

            async def close(self) -> None:
                return None

        async def exercise() -> StreamWaitResult:
            adapter = JsReverseMcpAdapter(SemanticTransport())
            return await adapter.wait_for_stream_condition(
                capture_id=7,
                request_matcher=RequestMatcher(url_contains="/conversation"),
                condition=WaitCondition(
                    type="first_event",
                    request_matcher=RequestMatcher(url_contains="/conversation"),
                ),
                checkpoint=StreamCheckpoint(
                    version=1,
                    event_indices={"req-semantic": -1},
                ),
                deadline=Deadline(2_000),
            )

        result = asyncio.run(exercise())
        self.assertTrue(result.condition_met)
        self.assertEqual(result.event_indices["req-semantic"], 0)

    def test_old_done_event_cannot_satisfy_a_later_checkpoint(self) -> None:
        class CheckpointTransport:
            def __init__(self) -> None:
                self.status_calls = 0
                self.predicate_calls: list[dict[str, Any]] = []

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "get_stream_status":
                    raise AssertionError(name)
                if "eventPredicate" in arguments:
                    self.predicate_calls.append(arguments)
                    return {
                        "capture": {"captureId": 7, "version": 4},
                        "eventMatch": {
                            "matched": True,
                            "matchedEventIndex": 1,
                            "matchedRequestId": "req-checkpoint",
                            "matchedSource": "raw-stream",
                        },
                        "requests": [],
                    }
                self.status_calls += 1
                event_count = 1 if self.status_calls == 1 else 2
                return {
                    "capture": {
                        "captureId": 7,
                        "version": 3 if event_count == 1 else 4,
                    },
                    "requests": [
                        {
                            "cdpRequestId": "req-checkpoint",
                            "url": "https://example.test/conversation",
                            "method": "POST",
                            "resourceType": "fetch",
                            "status": "streaming",
                            "rawEventCount": event_count,
                            "semanticEventCount": 0,
                        }
                    ],
                }

            async def close(self) -> None:
                return None

        transport = CheckpointTransport()

        async def exercise() -> StreamWaitResult:
            adapter = JsReverseMcpAdapter(transport)
            return await adapter.wait_for_stream_condition(
                capture_id=7,
                request_matcher=RequestMatcher(url_contains="/conversation"),
                condition=WaitCondition(
                    type="event_predicate",
                    request_matcher=RequestMatcher(url_contains="/conversation"),
                    predicate=ExactDataPredicate(
                        type="exact_data",
                        value="[DONE]",
                    ),
                ),
                checkpoint=StreamCheckpoint(
                    version=2,
                    event_indices={"req-checkpoint": 0},
                ),
                deadline=Deadline(3_000),
            )

        result = asyncio.run(exercise())
        self.assertTrue(result.condition_met)
        self.assertGreaterEqual(transport.status_calls, 2)
        self.assertEqual(len(transport.predicate_calls), 1)
        self.assertEqual(
            transport.predicate_calls[0]["afterEventIndex"],
            0,
        )
        self.assertEqual(result.matched_event["matchedEventIndex"], 1)

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

    def test_action_experiment_summary_is_bounded(self) -> None:
        manifest = {
            "experiment_id": "exp_many",
            "session_id": "session_many",
            "status": "completed",
            "objective_integrity": "complete",
            "primary_requests": [
                {
                    "cdpRequestId": f"request-{index}",
                    "url": "https://example.test/" + ("x" * 5_000),
                    "method": "POST",
                    "status": "finished",
                    "coreArtifacts": [{"large": "y" * 10_000}],
                }
                for index in range(500)
            ],
            "network_summary": {"requests": ["large"] * 10_000},
            "artifacts": [{"payload": "z" * 10_000}] * 100,
            "warnings": ["w" * 2_000] * 100,
            "errors": [],
        }
        summary = BrowserActionService._experiment_summary(manifest)
        self.assertEqual(summary["primary_request_count"], 500)
        self.assertEqual(len(summary["primary_requests"]), 10)
        self.assertNotIn("network_summary", summary)
        self.assertNotIn("artifacts", summary)
        self.assertLess(len(json.dumps(summary).encode("utf-8")), 50_000)

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


if __name__ == "__main__":
    unittest.main()
