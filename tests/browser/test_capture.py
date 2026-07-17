from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.browser_service import (
    BrowserActionService,
    ExperimentStore,
)
from tests.browser.common import BrowserActionTestCase
from tests.fakes.browser import FakeJsReverse, FakePlaywright


class CaptureBrowserTests(BrowserActionTestCase):
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
            self.assertEqual(summary["execution"]["status"], "complete")
            self.assertEqual(summary["quality_summary"]["status"], "complete")
            self.assertEqual(summary["quality_summary"]["missing_evidence"], [])
            self.assertEqual(len(experiment["network_observations"]), 1)
            self.assertNotIn("objective_integrity", experiment)
            self.assertNotIn("collector_integrity", experiment)
            self.assertNotIn("primary_request_integrity", experiment)
            self.assertTrue(experiment["capture_health"]["collector_stopped"])
            self.assertFalse(experiment["capture_health"]["worker_coverage"])
            artifact_kinds = {item["kind"] for item in experiment["artifacts"]}
            self.assertIn("playwright_screenshot", artifact_kinds)
            self.assertIn("playwright_trace", artifact_kinds)
            self.assertTrue(all("completeness" in item for item in experiment["artifacts"]))
            self.assertIn(
                "page_screenshot",
                {item["kind"] for item in experiment["evidence"]},
            )
            self.assertTrue(experiment["network_summary"]["requests"])
            self.assertLess(
                events.index("js.start"),
                events.index("playwright.step:submit_resource"),
            )
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
            client, events, _ = self.make_client(Path(temp_dir), alignment_status="not_aligned")
            with client:
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "open_session",
                        {"session_id": "session_one"},
                    ),
                )
            self.assertEqual(response.status_code, 409)
            self.assertTrue(response.json()["error"]["dispatch_started"])
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
                    json=self.browser_request(
                        "close_session",
                        {"session_id": "session_one"},
                    ),
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(js.start_arguments["include_in_flight"])
            self.assertEqual(close.status_code, 200)
            self.assertIn("playwright.close", events)
            session = json.loads(
                (root / "sessions" / "session_one.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session["status"], "closed")

    def test_explicit_baseline_capture_flow_allows_zero_primary_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "capture_flow",
                        {
                            "session_id": "session_one",
                            "objective": "capture baseline page and network state",
                            "primary_request": {
                                "expected_min_matches": 0,
                                "expected_max_matches": 100,
                            },
                            "flow": [],
                            "execution_mode": "sync",
                        },
                    ),
                )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            experiment = json.loads(
                (root / body["result"]["manifest_relative_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(experiment["primary_request_matcher"]["expected_min_matches"], 0)
            self.assertEqual(experiment["steps"], [])
            self.assertEqual(body["operation"], "capture_flow")
            self.assertEqual(experiment["operation"], "capture_flow")
            self.assertEqual(response.json()["status"], "completed")
            self.assertEqual(experiment["execution"]["status"], "complete")
            self.assertEqual(experiment["quality_summary"]["status"], "complete")

    def test_supporting_failure_can_be_made_objective_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            payload = self.request_payload(request)
            payload["primary_request"]["allow_supporting_failures"] = False
            self.set_request_payload(request, payload)
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            experiment = json.loads(
                (Path(temp_dir) / body["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(experiment["execution"]["status"], "complete")
            self.assertEqual(experiment["quality_summary"]["status"], "failed")
            self.assertIn("collector", experiment["quality_summary"]["missing_evidence"])

    def test_job_mode_returns_running_then_completes_via_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            payload = self.request_payload(request)
            payload.pop("execution_mode")
            payload["job_timeout_ms"] = 30_000
            self.set_request_payload(request, payload)
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
                        json=self.browser_request(
                            "get_experiment",
                            {"experiment_id": experiment_id},
                        ),
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
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(js.aligned_page_ids, [None, "page_fake", "page_fake"])

    def test_capture_without_page_index_reuses_session_selected_tab(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, events, _ = self.make_client(Path(temp_dir))
            with client:
                opened = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "open_session",
                        {
                            "session_id": "session_one",
                            "target": {"page_index": 2},
                        },
                    ),
                )
                self.assertEqual(opened.status_code, 200, opened.text)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(events.count("playwright.select_page:2"), 2)
            self.assertNotIn("playwright.select_page:0", events)

    def test_capture_rejects_implicit_target_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            payload = self.request_payload(request)
            payload["target"]["start_url"] = "https://example.test/new"
            self.set_request_payload(request, payload)
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
                    json=self.browser_request(
                        "get_session",
                        {"session_id": "session_old"},
                    ),
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
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "partial")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["execution"]["status"], "complete")
            self.assertEqual(manifest["quality_summary"]["status"], "partial")
            self.assertEqual(
                manifest["quality_summary"]["required_completeness"]["raw_stream"],
                "partial",
            )

    def test_stop_failure_records_orphan_capture_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, _ = self.make_client(root, fail_stop=True)
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "failed")
            self.assertIn("js.stop", events)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["capture_health"]["orphan_capture_id"], 1)
            self.assertEqual(
                manifest["capture_health"]["collector_cleanup"],
                "unknown",
            )

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
            payload = self.request_payload(request)
            payload["flow"] = [
                {
                    "step_id": "wait_stream_started",
                    "action": "wait",
                    "condition": {
                        "type": "first_event",
                        "request_matcher": {"url_contains": "/api/resource"},
                    },
                },
                {
                    "step_id": "stop_generation",
                    "action": "click",
                    "locator": {"role": "button", "name": "Stop"},
                    "intent": "stop_generation",
                },
            ]
            payload["wait_for"] = {
                "type": "network_canceled",
                "request_matcher": {"url_contains": "/api/resource"},
            }
            self.set_request_payload(request, payload)
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            classification = manifest["cancellation_classifications"][0]
            self.assertEqual(
                classification["classification"],
                "unclassified_network_cancel",
            )
            self.assertFalse(classification["page_remained_aligned"])
