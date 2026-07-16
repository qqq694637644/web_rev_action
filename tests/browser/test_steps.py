from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.browser.common import BrowserActionTestCase


class StepsBrowserTests(BrowserActionTestCase):
    def test_step_failure_does_not_pollute_empty_quality_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                fail_step="snapshot_only",
                include_supporting_failure=False,
            )
            request = self.browser_request(
                "capture_flow",
                {
                    "session_id": "session_one",
                    "objective": "separate execution failure from evidence quality",
                    "primary_request": {
                        "expected_min_matches": 0,
                        "expected_max_matches": 100,
                    },
                    "capture": {
                        "network": False,
                        "stream": False,
                        "trace": False,
                        "screenshots": False,
                        "page_snapshots": False,
                        "console_errors": False,
                    },
                    "requirements": {
                        "require_raw_capture": False,
                        "require_semantic_parse": False,
                        "require_request_snapshot": False,
                        "require_artifacts": False,
                    },
                    "flow": [
                        {
                            "step_id": "snapshot_only",
                            "action": "snapshot",
                        }
                    ],
                    "execution_mode": "sync",
                    "deadline_ms": 10_000,
                },
            )
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "failed")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["execution"]["status"], "failed")
            self.assertTrue(manifest["execution"]["errors"])
            self.assertEqual(
                manifest["quality_summary"],
                {
                    "status": "complete",
                    "observation_count": 0,
                    "expected_observation_count": {"min": 0, "max": 100},
                    "count_satisfied": True,
                    "required_completeness": {},
                    "missing_evidence": [],
                    "errors": [],
                },
            )

    def test_stop_intent_correlates_only_the_primary_network_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(
                Path(temp_dir),
                include_supporting_failure=False,
                primary_status="canceled",
            )
            request = self.capture_request()
            payload = self.request_payload(request)
            payload["flow"] = [
                {
                    "step_id": "wait_stream_started",
                    "action": "wait",
                    "condition": {
                        "type": "first_event",
                        "request_matcher": {
                            "url_contains": "/api/resource",
                            "method": "POST",
                        },
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
                "request_matcher": {
                    "url_contains": "/api/resource",
                    "method": "POST",
                },
            }
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
            classification = experiment["cancellation_classifications"][0]
            self.assertEqual(classification["classification"], "expected_user_cancel")
            self.assertTrue(classification["within_stop_window"])
            self.assertEqual(
                experiment["network_observations"][0]["facts"][
                    "experiment_cancellation_classification"
                ],
                "expected_user_cancel",
            )
            self.assertIsNotNone(classification["stream_before_stop"])

    def test_stop_intent_without_stream_start_is_recorded_without_preclassification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                include_supporting_failure=False,
                primary_status="canceled",
            )
            request = self.capture_request()
            payload = self.request_payload(request)
            payload["flow"] = [
                {
                    "step_id": "stop_generation",
                    "action": "click",
                    "locator": {"role": "button", "name": "Stop"},
                    "intent": "stop_generation",
                }
            ]
            payload["wait_for"] = {
                "type": "network_canceled",
                "request_matcher": {"url_contains": "/api/resource"},
            }
            self.set_request_payload(request, payload)
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["cancellation_classifications"][0]["classification"],
                "unclassified_network_cancel",
            )
            self.assertFalse(manifest["cancellation_classifications"][0]["same_request_observed"])
