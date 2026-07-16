from __future__ import annotations

import json
import tempfile
from pathlib import Path

from skill_temple.browser.artifacts import ExperimentStore
from skill_temple.browser.core import BrowserServiceError, Deadline
from skill_temple.browser_service import (
    BrowserActionService,
)
from tests.browser.common import BrowserActionTestCase


class InspectionBrowserTests(BrowserActionTestCase):
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
