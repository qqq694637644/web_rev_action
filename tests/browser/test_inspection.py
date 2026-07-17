from __future__ import annotations

import json
import tempfile
from pathlib import Path

from skill_temple.browser.adapters.contracts import AdapterError
from skill_temple.browser.artifacts import ExperimentStore
from skill_temple.browser.core import BrowserServiceError, Deadline
from skill_temple.browser_service import (
    BrowserActionService,
)
from tests.browser.common import BrowserActionTestCase
from tests.fakes.browser import FakeJsReverse, FakePlaywright


class InspectionBrowserTests(BrowserActionTestCase):
    def test_real_inspect_adapter_error_is_structured_without_state_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                service = client.app.state.browser_action_service

                async def fail_current_page(session_id: str, deadline: Deadline) -> object:
                    raise AdapterError(
                        "inspect transport failed",
                        dispatch_started=True,
                        outcome_unknown=True,
                    )

                service.playwright.current_page = fail_current_page
                response = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "search_scripts",
                        {"session_id": "session_one", "query": "fetch"},
                    ),
                )

            self.assertEqual(response.status_code, 502, response.text)
            error = response.json()["error"]
            self.assertEqual(error["code"], "browser_adapter_failed")
            self.assertTrue(error["dispatch_started"])
            self.assertEqual(error["outcome"], "failed")
            self.assertEqual(error["session_id"], "session_one")

    def test_invalid_capture_metadata_is_written_as_manifest_warning(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            store = ExperimentStore(root)
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, root),
                experiments=store,
                default_browser_endpoint="http://127.0.0.1:9222",
            )
            experiment_id, directory, manifest = store.create_experiment(
                session_id="session_one",
                operation="capture_flow",
                objective="discover malformed capture metadata",
                deadline=Deadline(1_000),
                experiment_id="exp_bad_capture",
            )
            metadata = directory / "js-reverse" / "capture-bad" / "capture.json"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("{broken", encoding="utf-8")
            runtime_warnings: list[str] = []

            discovered = service._discover_capture_metadata(
                experiment_id,
                manifest,
                runtime_warnings,
            )
            saved = store.load_manifest(experiment_id)

            self.assertIsNone(discovered)
            self.assertEqual(len(runtime_warnings), 1)
            self.assertIn("capture-bad/capture.json", runtime_warnings[0])
            self.assertIn("JSONDecodeError", runtime_warnings[0])
            self.assertIn(runtime_warnings[0], saved["warnings"])

    def test_invalid_manifest_is_visible_without_hiding_valid_experiments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad = root / "experiments" / "exp_bad" / "manifest.json"
            bad.parent.mkdir(parents=True)
            bad.write_text("{not-json", encoding="utf-8")
            store = ExperimentStore(root)
            experiment_id, _, manifest = store.create_experiment(
                session_id="session_ok",
                operation="capture_flow",
                objective="valid experiment",
                deadline=Deadline(1_000),
                experiment_id="exp_ok",
            )
            manifest["status"] = "completed"
            store.write_manifest(experiment_id, manifest)

            listing = store.list_experiments(None, 1)

            self.assertEqual(
                [item["experiment_id"] for item in listing["experiments"]],
                ["exp_ok"],
            )
            self.assertEqual(len(listing["manifest_errors"]), 1)
            invalid = listing["manifest_errors"][0]
            self.assertEqual(invalid["status"], "manifest_invalid")
            self.assertEqual(invalid["manifest_error"]["path"], "experiments/exp_bad/manifest.json")
            self.assertEqual(invalid["manifest_error"]["error_type"], "JSONDecodeError")
            with self.assertRaises(BrowserServiceError) as raised:
                store.load_manifest("exp_bad")
            self.assertEqual(raised.exception.code, "manifest_invalid")

    def test_list_experiments_action_returns_manifest_errors_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root)
            bad = root / "experiments" / "exp_bad" / "manifest.json"
            bad.parent.mkdir(parents=True)
            bad.write_text("{broken", encoding="utf-8")
            with client:
                response = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request("list_experiments", {"limit": 1}),
                )
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()["result"]
            self.assertEqual(result["count"], 0)
            self.assertEqual(result["manifest_error_count"], 1)
            self.assertEqual(
                result["manifest_errors"][0]["manifest_error"]["path"],
                "experiments/exp_bad/manifest.json",
            )

    def test_action_experiment_summary_is_bounded(self) -> None:
        manifest = {
            "experiment_id": "exp_many",
            "session_id": "session_many",
            "status": "completed",
            "execution": {"status": "complete"},
            "quality_summary": {"status": "complete"},
            "network_observations": [
                {
                    "observation_id": f"obs-{index}",
                    "facts": {
                        "url": "https://example.test/" + ("x" * 5_000),
                        "method": "POST",
                        "status": "finished",
                    },
                    "association": {"confidence": "exact"},
                    "completeness": {"request_body": "complete"},
                    "missing_evidence": [],
                }
                for index in range(500)
            ],
            "network_summary": {"requests": ["large"] * 10_000},
            "artifacts": [{"payload": "z" * 10_000}] * 100,
            "warnings": ["w" * 2_000] * 100,
            "errors": [],
        }
        summary = BrowserActionService._experiment_summary(manifest)
        self.assertEqual(summary["network_observation_count"], 500)
        self.assertEqual(len(summary["network_observations"]), 10)
        self.assertNotIn("network_summary", summary)
        self.assertNotIn("artifacts", summary)
        self.assertLess(len(json.dumps(summary).encode("utf-8")), 50_000)
