from __future__ import annotations

import json

from skill_temple.browser_service import (
    BrowserActionService,
)
from tests.browser.common import BrowserActionTestCase


class InspectionBrowserTests(BrowserActionTestCase):
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
