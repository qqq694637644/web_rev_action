from __future__ import annotations

import json
import tempfile
from pathlib import Path

from skill_temple.browser.adapters.contracts import AdapterError
from tests.browser.common import BrowserActionTestCase
from tests.fakes.browser import FakeJsReverse


class ReplayExecutionBrowserTests(BrowserActionTestCase):
    def test_replay_adapter_failure_preserves_real_dispatch_fact_and_manifest(self) -> None:
        for dispatch_started, outcome_unknown, expected_code, expected_status in [
            (False, False, "browser_adapter_failed", "failed"),
            (True, True, "operation_outcome_unknown", "partial"),
        ]:
            with (
                self.subTest(dispatch_started=dispatch_started),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                client, events, _ = self.make_client(root, include_supporting_failure=False)
                with client:
                    self.open_session(client)
                    source_id, source_evidence, _ = self.capture_replay_source(client, root)

                    class FailingReplayJs(FakeJsReverse):
                        sent = dispatch_started
                        unknown = outcome_unknown

                        async def evaluate_browser_replay(
                            self,
                            spec_file: Path,
                            output_file: Path,
                            deadline: object,
                        ) -> dict[str, object]:
                            raise AdapterError(
                                "fake replay transport failure",
                                dispatch_started=self.sent,
                                outcome_unknown=self.unknown,
                            )

                    service = client.app.state.browser_action_service
                    service.js_reverse = FailingReplayJs(
                        events,
                        root,
                        include_supporting_failure=False,
                    )
                    before = set((root / "experiments").iterdir())
                    response = client.post(
                        "/v1/browser/run",
                        json=self.browser_request(
                            "replay_request",
                            {
                                "session_id": "session_one",
                                "objective": "classify replay dispatch boundary",
                                "source": {
                                    "experiment_id": source_id,
                                    "evidence_id": source_evidence["evidence_id"],
                                },
                                "execution_mode": "sync",
                                "deadline_ms": 10_000,
                            },
                        ),
                    )
                self.assertEqual(response.status_code, 502, response.text)
                error = response.json()["error"]
                self.assertEqual(error["code"], expected_code)
                self.assertEqual(error["dispatch_started"], dispatch_started)
                self.assertEqual(error["session_id"], "session_one")
                self.assertTrue(error["experiment_id"].startswith("exp_"))
                self.assertEqual(
                    error["manifest_relative_path"],
                    f"experiments/{error['experiment_id']}/manifest.json",
                )
                created = list(set((root / "experiments").iterdir()) - before)
                self.assertEqual([item.name for item in created], [error["experiment_id"]])
                manifest = json.loads(
                    (root / error["manifest_relative_path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(manifest["status"], expected_status)
                self.assertEqual(
                    manifest["operation_outcome"],
                    "unknown" if outcome_unknown else "failed",
                )
                replay_step = next(
                    item for item in manifest["steps"] if item["step_id"] == "replay_request"
                )
                self.assertEqual(
                    replay_step["status"],
                    "outcome_unknown" if outcome_unknown else "failed",
                )

    def test_generic_replay_supports_bindings_and_multiple_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                payload = {
                    "session_id": "session_one",
                    "objective": "generic replay with explicit inputs",
                    "source": {
                        "experiment_id": source_id,
                        "evidence_id": source_evidence["evidence_id"],
                    },
                    "mutations": [
                        {"type": "remove_json_path", "path": "/tracking_id"},
                        {
                            "type": "add_json_path",
                            "path": "/experimental_flag",
                            "value": True,
                        },
                    ],
                    "bindings": [
                        {
                            "binding_id": "record_id",
                            "target": "json_pointer",
                            "path": "/records/0/id",
                            "value_source": "generated",
                            "generator": "uuid4",
                        },
                        {
                            "binding_id": "tracking_header",
                            "target": "header",
                            "name": "Content-Type",
                            "value_source": "literal",
                            "value": "application/json",
                        },
                        {
                            "binding_id": "manual_model",
                            "target": "json_pointer",
                            "path": "/model",
                            "value_source": "manual_input",
                            "value": "stage-d-private-model-value",
                        },
                    ],
                    "execution_mode": "sync",
                    "deadline_ms": 10_000,
                    "capture": {
                        "network": True,
                        "stream": False,
                        "trace": False,
                        "screenshots": False,
                        "page_snapshots": False,
                        "console_errors": False,
                    },
                }
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request("replay_request", payload),
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "completed")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            replay = manifest["replay"]
            self.assertNotIn("replay_mode", replay)
            self.assertNotIn("pair_protocol", replay)
            self.assertEqual(len(replay["bindings"]), 3)
            self.assertNotIn("binding_values", replay)
            self.assertTrue(
                all(
                    "value_sha256" in item
                    for item in replay["binding_observations"]
                    if item["resolved"]
                )
            )
            self.assertNotIn(
                "stage-d-private-model-value",
                json.dumps(manifest, ensure_ascii=False),
            )
            self.assertEqual(
                len(manifest["mutation_assessment"]["mutations"]),
                2,
            )
            self.assertEqual(manifest["comparison_results"], [])
            replay_network_id = replay["network_evidence_id"]
            replay_network = next(
                item
                for item in manifest["evidence"]
                if item.get("evidence_id") == replay_network_id
            )
            self.assertEqual(replay_network["request_ids"]["reqid"], 3)

    def test_custom_network_evidence_keeps_mandatory_replay_selector(self) -> None:
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
                            "objective": "capture exact replay plus supporting traffic",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "network_evidence": [
                                {
                                    "selector_id": "supporting_only",
                                    "matcher": {
                                        "url_contains": "/unrelated-supporting",
                                        "method": "GET",
                                    },
                                    "export_parts": ["all"],
                                }
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
            replay = manifest["replay"]
            self.assertIsNotNone(replay["network_evidence_id"])
            self.assertEqual(
                [
                    item["selector_id"]
                    for item in replay["requested_replay_protocol"]["network_evidence"]
                ],
                ["supporting_only"],
            )
            self.assertEqual(
                [
                    item["selector_id"]
                    for item in replay["replay_protocol"]["network_evidence"]
                ],
                ["replay_request", "supporting_only"],
            )
            exact = next(
                item
                for item in manifest["evidence"]
                if item.get("evidence_id") == replay["network_evidence_id"]
            )
            self.assertEqual(exact["selector_id"], "replay_request")

    def test_overwritten_bindings_and_mutations_are_applied_not_ineffective(self) -> None:
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
                            "objective": "audit ordered overlapping replay operations",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "bindings": [
                                {
                                    "binding_id": "cursor",
                                    "target": "json_pointer",
                                    "path": "/cursor_id",
                                    "value_source": "literal",
                                    "value": "bound-cursor",
                                }
                            ],
                            "mutations": [
                                {
                                    "type": "replace_json_path",
                                    "path": "/cursor_id",
                                    "value": "first-cursor",
                                },
                                {
                                    "type": "replace_json_path",
                                    "path": "/cursor_id",
                                    "value": "final-cursor",
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
            assessment = manifest["mutation_assessment"]
            self.assertTrue(assessment["all_mutations_applied_to_spec"])
            self.assertTrue(assessment["all_mutations_effective"])
            first, second = assessment["mutations"]
            self.assertIsNone(first["mutation_effective"])
            self.assertEqual(
                first["final_wire_observability"],
                "overwritten_by_later_operation",
            )
            self.assertTrue(second["mutation_effective"])
            binding = assessment["bindings"]
            self.assertTrue(binding["binding_application_complete"])
            self.assertEqual(
                binding["binding_observations"][0]["final_wire_observability"],
                "overwritten_by_later_operation",
            )

    def test_generic_replay_fails_closed_on_ambiguous_exact_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                js.duplicate_next_replay_requests = 1
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "replay_request",
                        {
                            "session_id": "session_one",
                            "objective": "reject ambiguous exact replay candidates",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
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
            self.assertIsNone(manifest["replay"]["network_evidence_id"])
            self.assertTrue(
                any("ambiguous" in item.lower() for item in manifest["execution"]["errors"])
            )

    def test_generic_replay_requires_observed_timestamp_for_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                js.omit_observed_at_reqids.add(3)
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "replay_request",
                        {
                            "session_id": "session_one",
                            "objective": "require a bounded correlation timestamp",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
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
            self.assertTrue(
                any(
                    "dispatch window" in item.lower()
                    for item in manifest["execution"]["errors"]
                )
            )
