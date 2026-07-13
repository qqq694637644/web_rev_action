from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.current_site_inventory import InventoryError, generate_reports, load_manifests


class CurrentSiteInventoryTests(unittest.TestCase):
    def write_manifest(self, root: Path, experiment_id: str, value: dict) -> None:
        target = root / "experiments" / experiment_id
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )

    def sample_manifest(self) -> dict:
        return {
            "experiment_id": "exp_current",
            "session_id": "session_current",
            "operation": "capture_flow",
            "objective": "inventory current site",
            "status": "completed",
            "created_at": "2026-07-13T12:00:00Z",
            "execution": {"status": "complete"},
            "quality_summary": {
                "status": "complete",
                "observation_count": 1,
                "missing_evidence": [],
            },
            "series": {
                "analysis_series_id": "series_current",
                "scenario_type": "reconnaissance",
            },
            "page_alignment": {
                "status": "aligned",
                "playwright_page": {
                    "url": "https://app.example.test/workspace?region=us",
                    "title": "Current App",
                },
                "js_reverse_page_url": "https://app.example.test/workspace?region=us",
            },
            "post_flow_alignment": {
                "status": "aligned",
                "playwright_page": {
                    "url": "https://app.example.test/workspace/thread/42",
                    "title": "Current App",
                },
            },
            "steps": [
                {
                    "step_id": "submit_probe",
                    "status": "completed",
                    "snapshot_ref": "experiments/exp_current/playwright/after.yaml",
                }
            ],
            "snapshot_paths": ["experiments/exp_current/playwright/after.yaml"],
            "evidence": [
                {
                    "evidence_id": "ev_network",
                    "kind": "network_request",
                    "selector_id": "primary",
                    "summary": {
                        "url": ("https://api.example.test/v2/stream?cursor=secret-value&mode=live"),
                        "method": "POST",
                        "resource_type": "fetch",
                        "status": 200,
                        "request_headers": [
                            {"name": "Authorization", "value": "<redacted>"},
                            {"name": "X-Client-Secret", "value": "<redacted>"},
                            {"name": "Content-Type", "value": "application/json"},
                        ],
                        "response_headers": [
                            {"name": "Content-Type", "value": "text/event-stream; charset=utf-8"}
                        ],
                        "request_shape": {
                            "format": "json-pointer-v1",
                            "paths": {
                                "/": {"type": "object"},
                                "/clientRequestId": {
                                    "type": "string",
                                    "value": "<identifier>",
                                },
                                "/payload": {"type": "object"},
                            },
                        },
                    },
                },
                {
                    "evidence_id": "ev_stream",
                    "kind": "stream_request",
                    "summary": {
                        "url": "https://api.example.test/v2/stream?cursor=secret-value",
                        "method": "POST",
                        "status": "finished",
                        "terminal_reason": "done_marker",
                        "primary_event_source": "raw-stream",
                        "raw_event_count": 4,
                        "semantic_event_count": 4,
                        "raw_capture_integrity": "complete",
                        "semantic_parse_integrity": "complete",
                        "stream_artifact_integrity": "complete",
                    },
                },
                {
                    "evidence_id": "ev_console",
                    "kind": "console_message",
                    "summary": {"type": "warn", "text": "fixture warning"},
                },
                {
                    "evidence_id": "ev_source",
                    "kind": "script_source",
                },
            ],
            "network_observations": [
                {
                    "observation_id": "obs_current",
                    "facts": {
                        "url": "https://api.example.test/v2/stream?cursor=secret-value",
                        "method": "POST",
                        "status": 200,
                    },
                    "association": {
                        "status": "matched",
                        "method": "network_request_id",
                        "confidence": "exact",
                    },
                    "completeness": {
                        "request_headers": "complete",
                        "request_body": "complete",
                        "raw_stream": "complete",
                        "semantic_stream": "complete",
                        "stream_artifacts": "complete",
                    },
                    "missing_evidence": [],
                }
            ],
        }

    def test_generate_reports_uses_manifest_facts_without_query_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.write_manifest(root, "exp_current", self.sample_manifest())
            output = root / "derived-reports"

            paths = generate_reports(
                root,
                output,
                session_id="session_current",
                analysis_series_id="series_current",
            )

            self.assertEqual(
                set(paths),
                {
                    "current-site-inventory.md",
                    "current-ui-map.md",
                    "current-network-map.md",
                    "open-questions.md",
                },
            )
            inventory = paths["current-site-inventory.md"].read_text(encoding="utf-8")
            ui_map = paths["current-ui-map.md"].read_text(encoding="utf-8")
            network_map = paths["current-network-map.md"].read_text(encoding="utf-8")
            questions = paths["open-questions.md"].read_text(encoding="utf-8")

            self.assertIn("series_current", inventory)
            self.assertIn("Canonical network observations: 1", inventory)
            self.assertIn("Authorization", inventory)
            self.assertIn("X-Client-Secret", inventory)
            self.assertIn("/clientRequestId", inventory)
            self.assertIn("https://app.example.test/workspace", ui_map)
            self.assertIn("submit_probe", ui_map)
            self.assertIn("https://api.example.test/v2/stream", network_map)
            self.assertIn("SSE over Fetch", network_map)
            self.assertIn("done_marker", network_map)
            self.assertIn("Saved script-source evidence is present", questions)
            for content in (inventory, ui_map, network_map, questions):
                self.assertNotIn("secret-value", content)

    def test_stream_only_evidence_contributes_origin_and_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self.sample_manifest()
            manifest.pop("page_alignment")
            manifest.pop("post_flow_alignment")
            manifest["evidence"] = [
                {
                    "evidence_id": "ev_stream_only",
                    "kind": "stream_request",
                    "summary": {
                        "url": "https://stream-only.example.test/events?cursor=private",
                        "method": "GET",
                        "status": "finished",
                        "primary_event_source": "eventsource",
                    },
                }
            ]
            self.write_manifest(root, "exp_current", manifest)

            paths = generate_reports(root, root / "reports")
            inventory = paths["current-site-inventory.md"].read_text(encoding="utf-8")
            network_map = paths["current-network-map.md"].read_text(encoding="utf-8")

            self.assertIn("https://stream-only.example.test", inventory)
            self.assertIn("SSE (EventSource)", inventory)
            self.assertIn("https://stream-only.example.test/events", network_map)
            self.assertIn("SSE (EventSource)", network_map)
            self.assertNotIn("cursor=private", network_map)

    def test_unknown_sse_delivery_is_not_assigned_to_fetch_or_xhr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            manifest = self.sample_manifest()
            network = manifest["evidence"][0]["summary"]
            network.pop("resource_type")
            self.write_manifest(root, "exp_current", manifest)

            paths = generate_reports(root, root / "reports")
            network_map = paths["current-network-map.md"].read_text(encoding="utf-8")

            self.assertIn("SSE (delivery unknown)", network_map)
            self.assertNotIn("SSE over Fetch", network_map)
            self.assertNotIn("SSE over XHR", network_map)

    def test_filters_select_only_requested_series(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = self.sample_manifest()
            second = self.sample_manifest()
            second["experiment_id"] = "exp_other"
            second["session_id"] = "session_other"
            second["series"] = {"analysis_series_id": "series_other"}
            self.write_manifest(root, "exp_current", first)
            self.write_manifest(root, "exp_other", second)

            manifests, skipped = load_manifests(
                root,
                session_id="session_current",
                analysis_series_id="series_current",
            )

            self.assertEqual([item["experiment_id"] for item in manifests], ["exp_current"])
            self.assertEqual(skipped, [])

    def test_invalid_manifest_is_reported_but_valid_manifest_still_generates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.write_manifest(root, "exp_current", self.sample_manifest())
            invalid_dir = root / "experiments" / "exp_invalid"
            invalid_dir.mkdir(parents=True)
            (invalid_dir / "manifest.json").write_text("{not-json", encoding="utf-8")

            paths = generate_reports(root, root / "reports")
            inventory = paths["current-site-inventory.md"].read_text(encoding="utf-8")

            self.assertIn("exp_invalid/manifest.json", inventory)
            self.assertIn("JSONDecodeError", inventory)

    def test_no_matching_manifests_fails_without_empty_fact_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.write_manifest(root, "exp_current", self.sample_manifest())

            with self.assertRaisesRegex(InventoryError, "No valid experiment manifests"):
                generate_reports(root, root / "reports", session_id="missing")


if __name__ == "__main__":
    unittest.main()
