from __future__ import annotations

import json
import tempfile
from pathlib import Path

from skill_temple.telemetry import TelemetryRecorder, load_events, summarize_events
from tests.browser.common import BrowserActionTestCase


class TelemetryIntegrationTests(BrowserActionTestCase):
    def test_recorder_is_bounded_and_summary_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = TelemetryRecorder(temp_dir)
            recorder.record(
                "skill_load_completed",
                loaded_skill_count=2,
                loaded_skill_ids=["browser-action-protocol", "browser-request-replay"],
            )
            recorder.record(
                "browser_request_received", action="inspect", operation="get_session"
            )
            recorder.record(
                "browser_request_valid", action="inspect", operation="get_session"
            )
            events = load_events(recorder.path)
            summary = summarize_events(events)

            self.assertEqual(summary["event_count"], 3)
            self.assertEqual(
                summary["skill_metrics"]["average_loaded_skill_count"], 2.0
            )
            self.assertEqual(summary["browser_metrics"]["first_pass_valid_rate"], 1.0)
            self.assertEqual(
                summary["browser_metrics"]["operation_counts"], {"get_session": 1}
            )

    def test_action_telemetry_never_records_payload_or_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root)
            secret = "fixture-super-secret-cookie"
            request = self.browser_request(
                "list_experiments",
                {"session_id": "session_one", "limit": 10},
            )
            request["payload_json"] = json.dumps(
                {"session_id": "session_one", "limit": 10, "credential": secret}
            )
            response = client.post("/v1/browser/inspect", json=request)
            self.assertEqual(response.status_code, 422)

            telemetry_path = root / "telemetry" / "action-events.jsonl"
            content = telemetry_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, content)
            self.assertNotIn("payload_json", content)
            events = load_events(telemetry_path)
            self.assertTrue(
                any(
                    item.get("event") == "browser_request_error"
                    and item.get("code") == "invalid_operation_payload"
                    for item in events
                )
            )

    def test_capture_manifest_binds_transport_skill_and_operation_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run",
                    json=self.capture_request(),
                )
            self.assertEqual(response.status_code, 200, response.text)
            experiment_id = response.json()["experiment_id"]
            manifest = json.loads(
                (root / "experiments" / experiment_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["action_transport_version"], "2.0")
            self.assertEqual(manifest["operation"], "capture_flow")
            self.assertEqual(manifest["skill_id"], "browser-action-protocol")
            self.assertRegex(manifest["skill_content_hash"], r"^sha256:[a-f0-9]{64}$")
            self.assertRegex(
                manifest["operation_contract_hash"], r"^sha256:[a-f0-9]{64}$"
            )
