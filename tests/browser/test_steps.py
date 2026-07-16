from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from skill_temple.browser.adapters.contracts import StreamCheckpoint
from skill_temple.browser.core import Deadline
from skill_temple.browser.steps import StepExecutor
from skill_temple.browser_models import ClickStep, RequestMatcher
from tests.browser.common import BrowserActionTestCase


class StepsBrowserTests(BrowserActionTestCase):
    def test_mutation_cancellation_before_checkpoint_dispatch_is_not_unknown(self) -> None:
        class Experiments:
            @staticmethod
            def relative_path(value: str) -> str:
                return value

        class Playwright:
            async def execute_step(self, *args: object, **kwargs: object) -> dict[str, object]:
                raise AssertionError("mutation must not be dispatched")

        class Service:
            experiments = Experiments()
            playwright = Playwright()

            @staticmethod
            def _ensure_finalize_reserve(deadline: Deadline, label: str) -> None:
                return None

            @staticmethod
            def _operation_deadline(
                deadline: Deadline, requested_ms: int, label: str
            ) -> Deadline:
                return deadline

            @staticmethod
            async def _stream_checkpoint(*args: object, **kwargs: object) -> StreamCheckpoint:
                raise asyncio.CancelledError

        results: list[object] = []

        async def exercise() -> None:
            with self.assertRaises(asyncio.CancelledError):
                await StepExecutor.execute_many(
                    Service(),
                    phase="action",
                    steps=[
                        ClickStep(
                            step_id="click_after_checkpoint",
                            action="click",
                            locator={"role": "button", "name": "Send"},
                        )
                    ],
                    session_id="session_one",
                    experiment_dir=Path("."),
                    deadline=Deadline(5_000),
                    capture_id=1,
                    request_matcher=RequestMatcher(),
                    stream_checkpoint=StreamCheckpoint(),
                    first_mutation_wall_time_ms=None,
                    step_results=results,
                    wait_observations=[],
                )

        asyncio.run(exercise())
        self.assertEqual(results[0].status, "canceled")
        self.assertIn("before confirmed dispatch", results[0].error)

    def test_mutation_cancellation_after_adapter_dispatch_is_unknown(self) -> None:
        class Experiments:
            @staticmethod
            def relative_path(value: str) -> str:
                return value

        class Playwright:
            async def execute_step(self, *args: object, **kwargs: object) -> dict[str, object]:
                error = asyncio.CancelledError()
                error.adapter_dispatch_started = True
                raise error

        class Service:
            experiments = Experiments()
            playwright = Playwright()

            @staticmethod
            def _ensure_finalize_reserve(deadline: Deadline, label: str) -> None:
                return None

            @staticmethod
            def _operation_deadline(
                deadline: Deadline, requested_ms: int, label: str
            ) -> Deadline:
                return deadline

        results: list[object] = []

        async def exercise() -> None:
            with self.assertRaises(asyncio.CancelledError):
                await StepExecutor.execute_many(
                    Service(),
                    phase="action",
                    steps=[
                        ClickStep(
                            step_id="sent_click",
                            action="click",
                            locator={"role": "button", "name": "Send"},
                        )
                    ],
                    session_id="session_one",
                    experiment_dir=Path("."),
                    deadline=Deadline(5_000),
                    capture_id=None,
                    request_matcher=RequestMatcher(),
                    stream_checkpoint=StreamCheckpoint(),
                    first_mutation_wall_time_ms=None,
                    step_results=results,
                    wait_observations=[],
                )

        asyncio.run(exercise())
        self.assertEqual(results[0].status, "canceled_outcome_unknown")
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
