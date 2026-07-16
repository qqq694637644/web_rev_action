from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tests.browser.common import BrowserActionTestCase


class ReplayExtractorsBrowserTests(BrowserActionTestCase):
    def test_extractors_feed_bindings_and_failures_are_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.setup_output_response = {"resource": {"id": "created-resource-id"}}
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "replay_request",
                        {
                            "session_id": "session_one",
                            "objective": "extract setup response and bind it",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "setup_flow": [
                                {
                                    "step_id": "setup_create",
                                    "action": "navigate",
                                    "value": "https://example.test/create",
                                }
                            ],
                            "extractors": [
                                {
                                    "extractor_id": "resource_id",
                                    "type": "network_response_json",
                                    "selector": {
                                        "url_contains": "/api/setup-resource",
                                        "method": "POST",
                                    },
                                    "pointer": "/resource/id",
                                },
                                {
                                    "extractor_id": "optional_missing",
                                    "type": "network_response_json",
                                    "selector": {
                                        "url_contains": "/not-observed",
                                        "method": "POST",
                                    },
                                    "pointer": "/id",
                                    "required": False,
                                },
                                {
                                    "extractor_id": "optional_pointer_missing",
                                    "type": "network_response_json",
                                    "selector": {
                                        "url_contains": "/api/setup-resource",
                                        "method": "POST",
                                    },
                                    "pointer": "/resource/not_present",
                                    "required": False,
                                },
                            ],
                            "bindings": [
                                {
                                    "binding_id": "resource_id",
                                    "target": "json_pointer",
                                    "path": "/cursor_id",
                                    "value_source": "extractor",
                                    "extractor_id": "resource_id",
                                },
                                {
                                    "binding_id": "optional_missing",
                                    "target": "header",
                                    "name": "X-Optional",
                                    "value_source": "extractor",
                                    "extractor_id": "optional_missing",
                                },
                            ],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    ),
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "completed")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            observations = manifest["replay"]["extractor_observations"]
            self.assertEqual(observations[0]["status"], "completed")
            self.assertEqual(observations[1]["status"], "failed")
            self.assertEqual(observations[2]["status"], "failed")
            self.assertIn("optional_missing", manifest["replay"]["unresolved_binding_ids"])
            self.assertEqual(manifest["quality_summary"]["status"], "complete")
            extractor_evidence = [
                item for item in manifest["evidence"] if item.get("kind") == "replay_extractor"
            ]
            self.assertEqual(len(extractor_evidence), 3)
            self.assertNotIn("snapshot_relative_path", observations[0])
            artifact_id_value = observations[0]["artifact_ids"][0]
            artifact = next(
                item
                for item in manifest["artifacts"]
                if item.get("artifactId") == artifact_id_value
            )
            self.assertEqual(artifact["kind"], "replay_extractor_snapshot")
            self.assertEqual(artifact["sensitivity"], "credential")
            self.assertTrue(artifact["containsCredentials"])
            self.assertEqual(artifact["completeness"], "complete")
            completed_evidence = next(
                item
                for item in extractor_evidence
                if item["summary"]["extractor_id"] == "resource_id"
            )
            self.assertEqual(completed_evidence["artifact_ids"], [artifact_id_value])
            pointer_failure = observations[2]
            self.assertTrue(pointer_failure["artifact_ids"])
            pointer_failure_artifact = next(
                item
                for item in manifest["artifacts"]
                if item.get("artifactId") == pointer_failure["artifact_ids"][0]
            )
            self.assertEqual(pointer_failure_artifact["sensitivity"], "credential")
            pointer_failure_evidence = next(
                item
                for item in extractor_evidence
                if item["summary"]["extractor_id"] == "optional_pointer_missing"
            )
            self.assertEqual(
                pointer_failure_evidence["artifact_ids"],
                pointer_failure["artifact_ids"],
            )

    def test_required_extractor_failure_affects_quality_not_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "replay_request",
                        {
                            "session_id": "session_one",
                            "objective": "record one required extractor failure",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "extractors": [
                                {
                                    "extractor_id": "required_missing",
                                    "type": "network_response_json",
                                    "selector": {
                                        "url_contains": "/not-observed",
                                        "method": "POST",
                                    },
                                    "pointer": "/id",
                                    "required": True,
                                }
                            ],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    ),
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "failed")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["execution"]["status"], "complete")
            self.assertEqual(manifest["quality_summary"]["status"], "failed")
            self.assertEqual(
                manifest["quality_summary"]["errors"],
                ["required_extractor_failed:required_missing"],
            )
            self.assertIn(
                "extractor:required_missing",
                manifest["quality_summary"]["missing_evidence"],
            )
