from __future__ import annotations

import json
import tempfile
from pathlib import Path

from skill_temple.protocol_evidence import build_network_observation
from tests.browser.common import BrowserActionTestCase


class ReplayComparisonBrowserTests(BrowserActionTestCase):
    def test_stream_only_response_status_comparison_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            service = client.app.state.browser_action_service
            observation = build_network_observation(
                observation_id="obs_stream_only",
                network_evidence=None,
                stream_request={
                    "status": "finished",
                    "terminalReason": "network_close",
                    "rawEventCount": 3,
                    "semanticEventCount": 3,
                    "primaryEventSource": "fetch-stream",
                    "rawCaptureIntegrity": "complete",
                    "semanticParseIntegrity": "complete",
                    "artifactIntegrity": "complete",
                },
                association={"status": "not_found", "method": None},
            )
            experiment_id = "exp_stream_only"
            service.experiments.experiment_dir(experiment_id).mkdir(
                parents=True,
                exist_ok=True,
            )
            service.experiments.write_manifest(
                experiment_id,
                {
                    "experiment_id": experiment_id,
                    "network_observations": [observation],
                },
            )

            results = service._build_replay_comparison_results(
                {
                    "comparison": {
                        "references": [
                            {
                                "experiment_id": experiment_id,
                                "observation_id": "obs_stream_only",
                            }
                        ],
                        "dimensions": ["response_status"],
                    }
                },
                current_request_body_sha256=None,
                current_response_status=200,
                current_response_content_type=None,
                current_stream_facts=None,
                current_environment=None,
            )

            self.assertEqual(results[0]["status"], "missing")
            self.assertEqual(
                results[0]["dimensions"]["response_status"],
                {"status": "missing", "reference": None, "current": 200},
            )

    def test_stream_summary_uses_one_canonical_shape_on_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            service = client.app.state.browser_action_service
            observation = build_network_observation(
                observation_id="obs_stream_reference",
                network_evidence={
                    "evidence_id": "ev_stream_reference",
                    "summary": {
                        "url": "https://example.test/stream",
                        "method": "POST",
                        "status": 200,
                    },
                },
                stream_request={
                    "status": "finished",
                    "terminalReason": "network_close",
                    "rawEventCount": 3,
                    "semanticEventCount": 3,
                    "primaryEventSource": "fetch-stream",
                    "rawCaptureIntegrity": "complete",
                    "semanticParseIntegrity": "complete",
                    "artifactIntegrity": "complete",
                },
                association={"status": "matched", "method": "network_request_id"},
            )
            experiment_id = "exp_stream_reference"
            service.experiments.experiment_dir(experiment_id).mkdir(
                parents=True,
                exist_ok=True,
            )
            service.experiments.write_manifest(
                experiment_id,
                {
                    "experiment_id": experiment_id,
                    "network_observations": [observation],
                },
            )
            current_summary = service._stream_summary_from_observation(observation)
            replay_plan = {
                "comparison": {
                    "references": [
                        {
                            "experiment_id": experiment_id,
                            "observation_id": "obs_stream_reference",
                        }
                    ],
                    "dimensions": ["stream_summary"],
                }
            }

            equivalent = service._build_replay_comparison_results(
                replay_plan,
                current_request_body_sha256=None,
                current_response_status=200,
                current_response_content_type="text/event-stream",
                current_stream_facts=current_summary,
                current_environment=None,
            )
            different = service._build_replay_comparison_results(
                replay_plan,
                current_request_body_sha256=None,
                current_response_status=200,
                current_response_content_type="text/event-stream",
                current_stream_facts={
                    **current_summary,
                    "primary_event_source": "eventsource",
                },
                current_environment=None,
            )

            self.assertEqual(
                equivalent[0]["dimensions"]["stream_summary"]["status"],
                "equivalent",
            )
            self.assertEqual(
                different[0]["dimensions"]["stream_summary"]["status"],
                "different",
            )

            supporting = build_network_observation(
                observation_id="obs_supporting",
                network_evidence={
                    "evidence_id": "ev_supporting",
                    "summary": {
                        "url": "https://example.test/stream",
                        "method": "POST",
                        "status": 200,
                    },
                },
                stream_request={
                    "status": "finished",
                    "terminalReason": "idle_window",
                    "rawEventCount": 9,
                    "semanticEventCount": 9,
                    "primaryEventSource": "eventsource",
                    "rawCaptureIntegrity": "complete",
                    "semanticParseIntegrity": "complete",
                    "artifactIntegrity": "complete",
                },
                association={"status": "matched", "method": "network_request_id"},
            )
            selected, selected_status = service._current_replay_stream_summary(
                [supporting, observation],
                "ev_stream_reference",
            )
            missing, missing_status = service._current_replay_stream_summary(
                [supporting, observation],
                "ev_not_present",
            )
            duplicate = {**observation, "observation_id": "obs_stream_duplicate"}
            ambiguous, ambiguous_status = service._current_replay_stream_summary(
                [observation, duplicate],
                "ev_stream_reference",
            )
            ambiguous_result = service._build_replay_comparison_results(
                replay_plan,
                current_request_body_sha256=None,
                current_response_status=200,
                current_response_content_type="text/event-stream",
                current_stream_facts=ambiguous,
                current_environment=None,
                current_status_overrides={"stream_summary": ambiguous_status},
            )

            self.assertEqual(selected, current_summary)
            self.assertIsNone(selected_status)
            self.assertIsNone(missing)
            self.assertEqual(missing_status, "missing")
            self.assertIsNone(ambiguous)
            self.assertEqual(ambiguous_status, "ambiguous")
            self.assertEqual(
                ambiguous_result[0]["dimensions"]["stream_summary"]["status"],
                "ambiguous",
            )

    def test_include_source_compares_exact_source_evidence(self) -> None:
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
                            "objective": "compare replay with its exact capture source",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "comparison": {
                                "include_source": True,
                                "dimensions": [
                                    "request_body",
                                    "response_status",
                                    "response_content_type",
                                ],
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    ),
                )
            self.assertEqual(response.status_code, 200, response.text)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            result = manifest["comparison_results"][0]
            self.assertEqual(
                result["reference"],
                {
                    "experiment_id": source_id,
                    "evidence_id": source_evidence["evidence_id"],
                },
            )
            self.assertEqual(result["status"], "equivalent")
            self.assertEqual(
                result["dimensions"]["response_status"],
                {"status": "equivalent", "reference": 200, "current": 200},
            )
            self.assertNotEqual(
                result["dimensions"]["response_content_type"]["status"],
                "missing",
            )

    def test_replay_comparison_supports_arbitrary_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                baseline_payload = {
                    "session_id": "session_one",
                    "objective": "baseline observation",
                    "source": {
                        "experiment_id": source_id,
                        "evidence_id": source_evidence["evidence_id"],
                    },
                    "execution_mode": "sync",
                    "deadline_ms": 10_000,
                }
                baseline = client.post(
                    "/v1/browser/run",
                    json=self.browser_request("replay_request", baseline_payload),
                )
                self.assertEqual(baseline.status_code, 200, baseline.text)
                baseline_manifest = json.loads(
                    (root / baseline.json()["result"]["manifest_relative_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                baseline_network_evidence_id = baseline_manifest["replay"][
                    "network_evidence_id"
                ]
                compared_payload = {
                    "session_id": "session_one",
                    "objective": "compare one mutation with a prior replay",
                    "source": {
                        "experiment_id": source_id,
                        "evidence_id": source_evidence["evidence_id"],
                    },
                    "mutations": [
                        {"type": "remove_json_path", "path": "/tracking_id"}
                    ],
                    "comparison": {
                        "references": [
                            {
                                "experiment_id": baseline.json()["experiment_id"],
                                "evidence_id": baseline_network_evidence_id,
                            }
                        ],
                        "dimensions": [
                            "request_body",
                            "response_status",
                            "environment",
                        ],
                        "environment": {
                            "preset": "explicit",
                            "dimensions": ["page_origin"],
                        },
                    },
                    "execution_mode": "sync",
                    "deadline_ms": 10_000,
                }
                compared = client.post(
                    "/v1/browser/run",
                    json=self.browser_request("replay_request", compared_payload),
                )
            self.assertEqual(compared.status_code, 200, compared.text)
            manifest = json.loads(
                (root / compared.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            result = manifest["comparison_results"][0]
            self.assertEqual(result["reference_experiment_id"], baseline.json()["experiment_id"])
            self.assertEqual(
                result["dimensions"]["response_status"]["status"],
                "equivalent",
            )
            self.assertEqual(
                result["dimensions"]["request_body"]["status"],
                "different",
            )
            self.assertEqual(
                result["dimensions"]["environment"]["dimensions"]["page_origin"]["status"],
                "equivalent",
            )

    def test_error_statuses_are_valid_comparison_facts_and_analyzer_is_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "application/json"
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                js.replay_response_status = 422
                baseline = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "replay_request",
                        {
                            "session_id": "session_one",
                            "objective": "observe a validation response",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "response_reader": {
                                "analyzer": {
                                    "name": "http_response_classifier",
                                    "version": "1",
                                }
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    ),
                )
                self.assertEqual(baseline.status_code, 200, baseline.text)
                self.assertEqual(baseline.json()["status"], "completed")
                baseline_manifest = json.loads(
                    (root / baseline.json()["result"]["manifest_relative_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(baseline_manifest["replay_http_status"], 422)
                self.assertEqual(baseline_manifest["execution"]["status"], "complete")
                analysis = self.replay_response_analysis(baseline_manifest)
                self.assertEqual(analysis["classification"], "unknown_rejection")
                self.assertNotIn("replay_response_analysis", baseline_manifest)

                js.replay_response_status = 500
                compared = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "replay_request",
                        {
                            "session_id": "session_one",
                            "objective": "compare a server response with validation",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "comparison": {
                                "references": [
                                    {
                                        "experiment_id": baseline.json()["experiment_id"],
                                        "evidence_id": baseline_manifest["replay"][
                                            "network_evidence_id"
                                        ],
                                    },
                                    {
                                        "experiment_id": "exp_missing_reference",
                                        "evidence_id": "ev_missing_reference",
                                    },
                                ],
                                "dimensions": ["response_status"],
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    ),
                )
            self.assertEqual(compared.status_code, 200, compared.text)
            self.assertEqual(compared.json()["status"], "completed")
            manifest = json.loads(
                (root / compared.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["replay_http_status"], 500)
            self.assertEqual(len(manifest["comparison_results"]), 2)
            first, second = manifest["comparison_results"]
            self.assertEqual(first["status"], "different")
            self.assertEqual(
                first["dimensions"]["response_status"],
                {"status": "different", "reference": 422, "current": 500},
            )
            self.assertEqual(second["status"], "missing")
            self.assertEqual(second["error"], "experiment_not_found")
