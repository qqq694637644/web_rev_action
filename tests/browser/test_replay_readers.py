from __future__ import annotations

import json
import tempfile
from pathlib import Path

from skill_temple.browser_service import (
    BrowserActionService,
)
from tests.browser.common import BrowserActionTestCase


class ReplayReadersBrowserTests(BrowserActionTestCase):
    def test_stream_contract_requires_terminal_match_consistency(self) -> None:
        replay_plan = {
            "source_is_stream": True,
            "spec": {
                "responseControl": {
                    "responseMode": "sse",
                    "terminalConditions": [{"type": "network_close"}],
                }
            },
        }
        contradictory = BrowserActionService._stream_response_contract(
            replay_plan,
            {
                "responseMode": "sse",
                "terminationReason": "network_close",
                "terminalConditionMatched": "idle_window",
                "truncated": False,
            },
            status=200,
            content_type="text/event-stream",
        )
        missing_marker_match = BrowserActionService._stream_response_contract(
            {
                "source_is_stream": True,
                "spec": {
                    "responseControl": {
                        "responseMode": "sse",
                        "doneMarker": "fixture-complete",
                        "terminalConditions": [
                            {"type": "exact_sse_data", "value": "fixture-complete"}
                        ],
                    }
                },
            },
            {
                "responseMode": "sse",
                "terminationReason": "done_marker",
                "terminalConditionMatched": None,
                "doneMarkerObserved": True,
                "truncated": False,
            },
            status=200,
            content_type="text/event-stream",
        )

        self.assertEqual(contradictory["status"], "partial")
        self.assertTrue(contradictory["termination_reason_matches_conditions"])
        self.assertFalse(
            contradictory["terminal_condition_matches_observed_termination"]
        )
        self.assertEqual(missing_marker_match["status"], "partial")
        self.assertFalse(
            missing_marker_match["terminal_condition_matches_observed_termination"]
        )

    def test_generic_replay_supports_source_without_content_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = None
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "replay a source without Content-Type",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "completed")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(manifest["replay_response_content_type"])
            self.assertFalse(manifest["replay"]["source_is_stream"])

    def test_generic_stream_replay_locks_to_exact_network_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            js.replay_done_marker_observed = True
            js.replay_termination_reason = "done_marker"
            js.extra_same_endpoint_stream = {
                "cdpRequestId": "supporting-cdp",
                "persistentRequestId": "supporting-persistent",
                "networkRequestId": "supporting-network",
                "collectorGeneration": 1,
                "url": "https://example.test/api/resource",
                "method": "POST",
                "resourceType": "fetch",
                "status": "finished",
                "terminalReason": "completed",
                "integrityStatus": "complete",
                "rawCaptureIntegrity": "complete",
                "semanticParseIntegrity": "complete",
                "requestSnapshotIntegrity": "complete",
                "artifactIntegrity": "complete",
                "responseObserved": True,
                "rawEventCount": 3,
                "semanticEventCount": 3,
                "primaryEventSource": "fetch-stream",
                "coreArtifacts": [],
            }
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "lock stream observation to exact replay evidence",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "response_reader": {"mode": "sse", "raw_only": True},
                            "termination": {
                                "conditions": [
                                    {"type": "exact_sse_data", "value": "fixture-complete"}
                                ]
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            replay_network_id = manifest["replay"]["network_evidence_id"]
            self.assertEqual(len(manifest["network_observations"]), 1)
            observation = manifest["network_observations"][0]
            self.assertEqual(
                observation["sources"]["network_evidence_id"], replay_network_id
            )
            self.assertNotEqual(
                observation["request_ids"]["persistent_request_id"],
                "supporting-persistent",
            )

    def test_response_reader_and_termination_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            js.replay_done_marker_observed = True
            js.replay_termination_reason = "done_marker"
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "read an SSE response explicitly",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "response_reader": {
                                "mode": "sse",
                                "raw_only": True,
                                "max_bytes": 65_536,
                                "max_events": 128,
                            },
                            "termination": {
                                "conditions": [
                                    {"type": "exact_sse_data", "value": "fixture-complete"},
                                    {
                                        "type": "network_close",
                                        "value": "ignored",
                                        "event_name": "ignored",
                                    },
                                ],
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["stream_response_contract"]["status"], "complete")
            self.assertTrue(manifest["replay"]["source_is_stream"])
            replay = manifest["replay"]
            protocol = replay["replay_protocol"]
            requested_protocol = replay["requested_replay_protocol"]
            self.assertEqual(protocol["response_reader"]["mode"], "sse")
            self.assertEqual(protocol["response_reader"]["max_events"], 128)
            self.assertFalse(requested_protocol["capture"]["stream"])
            self.assertFalse(
                requested_protocol["requirements"]["require_raw_capture"]
            )
            self.assertTrue(protocol["capture"]["stream"])
            self.assertTrue(protocol["requirements"]["require_raw_capture"])
            self.assertFalse(protocol["requirements"]["require_semantic_parse"])
            self.assertTrue(protocol["requirements"]["require_artifacts"])
            self.assertTrue(protocol["source_is_stream"])
            self.assertNotEqual(
                replay["requested_replay_protocol_hash"],
                replay["replay_protocol_hash"],
            )
            self.assertEqual(
                protocol["termination"]["conditions"],
                [
                    {"type": "exact_sse_data", "value": "fixture-complete"},
                    {"type": "network_close"},
                ],
            )

    def test_stream_contract_requires_observed_reader_mode_to_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            js.replay_done_marker_observed = True
            js.replay_termination_reason = "done_marker"
            js.replay_observed_response_mode = "ordinary"
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "reject a contradictory observed reader mode",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "response_reader": {"mode": "sse", "raw_only": True},
                            "termination": {
                                "conditions": [
                                    {"type": "exact_sse_data", "value": "fixture-complete"}
                                ]
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "partial")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            contract = manifest["stream_response_contract"]
            self.assertEqual(contract["status"], "partial")
            self.assertEqual(contract["response_mode"], "sse")
            self.assertEqual(contract["observed_response_mode"], "ordinary")
            self.assertFalse(contract["response_mode_matches"])

    def test_complete_http_500_sse_is_complete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            js.replay_done_marker_observed = True
            js.replay_termination_reason = "done_marker"
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                js.replay_response_status = 500
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "preserve a complete server-error stream",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "response_reader": {"mode": "sse", "raw_only": True},
                            "termination": {
                                "conditions": [
                                    {"type": "exact_sse_data", "value": "fixture-complete"}
                                ]
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "completed")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["replay_http_status"], 500)
            self.assertEqual(manifest["execution"]["status"], "complete")
            self.assertEqual(manifest["stream_response_contract"]["status"], "complete")
            self.assertEqual(manifest["quality_summary"]["status"], "complete")
            self.assertNotIn(
                "stream_terminal_contract",
                manifest["quality_summary"]["missing_evidence"],
            )

    def test_http_500_ndjson_and_raw_stream_remain_stream_evidence(self) -> None:
        cases = [
            ("ndjson", "application/x-ndjson"),
            ("raw_stream", "application/octet-stream"),
        ]
        for mode, content_type in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                client, _, js = self.make_client(root, include_supporting_failure=False)
                js.network_response_content_type = content_type
                js.replay_observed_response_mode = mode
                js.replay_termination_reason = "network_close"
                with client:
                    self.open_session(client)
                    source_id, source_evidence, _ = self.capture_replay_source(client, root)
                    js.replay_response_status = 500
                    response = client.post(
                        "/v1/browser/run",
                        json={
                            "operation": "replay_request",
                            "payload": {
                                "session_id": "session_one",
                                "objective": f"preserve a complete HTTP 500 {mode} response",
                                "source": {
                                    "experiment_id": source_id,
                                    "evidence_id": source_evidence["evidence_id"],
                                },
                                "response_reader": {"mode": mode, "raw_only": True},
                                "termination": {
                                    "conditions": [{"type": "network_close"}]
                                },
                                "execution_mode": "sync",
                                "deadline_ms": 10_000,
                            },
                        },
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "completed")
                manifest = json.loads(
                    (root / response.json()["result"]["manifest_relative_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(manifest["replay_http_status"], 500)
                self.assertFalse(manifest["non_stream_error_response_observed"])
                self.assertEqual(
                    manifest["stream_response_contract"]["status"],
                    "complete",
                )
                self.assertEqual(
                    manifest["stream_response_contract"]["observed_response_mode"],
                    mode,
                )
                self.assertEqual(manifest["quality_summary"]["status"], "complete")

    def test_explicit_stream_reader_allows_missing_content_type(self) -> None:
        for mode in ("sse", "ndjson"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                client, _, js = self.make_client(root, include_supporting_failure=False)
                js.network_response_content_type = None
                js.replay_observed_response_mode = mode
                js.replay_termination_reason = "network_close"
                with client:
                    self.open_session(client)
                    source_id, source_evidence, _ = self.capture_replay_source(client, root)
                    response = client.post(
                        "/v1/browser/run",
                        json={
                            "operation": "replay_request",
                            "payload": {
                                "session_id": "session_one",
                                "objective": f"read {mode} without Content-Type",
                                "source": {
                                    "experiment_id": source_id,
                                    "evidence_id": source_evidence["evidence_id"],
                                },
                                "response_reader": {"mode": mode, "raw_only": True},
                                "termination": {
                                    "conditions": [{"type": "network_close"}]
                                },
                                "execution_mode": "sync",
                                "deadline_ms": 10_000,
                            },
                        },
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "completed")
                manifest = json.loads(
                    (root / response.json()["result"]["manifest_relative_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                contract = manifest["stream_response_contract"]
                self.assertEqual(contract["status"], "complete")
                self.assertTrue(contract["response_mode_matches"])
                self.assertFalse(contract["content_type_matches_observed_mode"])
                self.assertFalse(contract["content_type_required_for_contract"])

    def test_auto_reader_rejects_content_type_mode_contradiction(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            js.replay_observed_response_mode = "ordinary"
            js.replay_termination_reason = "network_close"
            with client:
                self.open_session(client)
                source_id, source_evidence, _ = self.capture_replay_source(client, root)
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "detect an auto-reader Content-Type contradiction",
                            "source": {
                                "experiment_id": source_id,
                                "evidence_id": source_evidence["evidence_id"],
                            },
                            "response_reader": {"mode": "auto", "raw_only": True},
                            "termination": {
                                "conditions": [{"type": "network_close"}]
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "partial")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            contract = manifest["stream_response_contract"]
            self.assertEqual(contract["status"], "partial")
            self.assertTrue(contract["response_mode_matches"])
            self.assertFalse(contract["content_type_matches_observed_mode"])
            self.assertTrue(contract["content_type_required_for_contract"])

    def test_auto_reader_captures_response_that_changes_from_json_to_stream(self) -> None:
        cases = [
            ("sse", "text/event-stream", "done_marker"),
            ("ndjson", "application/x-ndjson", "network_close"),
        ]
        for mode, content_type, termination_reason in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                client, _, js = self.make_client(root, include_supporting_failure=False)
                js.network_response_content_type = "application/json"
                with client:
                    self.open_session(client)
                    source_id, source_evidence, _ = self.capture_replay_source(client, root)
                    js.network_response_content_type = content_type
                    js.replay_observed_response_mode = mode
                    js.replay_termination_reason = termination_reason
                    js.replay_done_marker_observed = mode == "sse"
                    termination = (
                        {"conditions": [{"type": "exact_sse_data", "value": "fixture-complete"}]}
                        if mode == "sse"
                        else {"conditions": [{"type": "network_close"}]}
                    )
                    response = client.post(
                        "/v1/browser/run",
                        json={
                            "operation": "replay_request",
                            "payload": {
                                "session_id": "session_one",
                                "objective": f"auto-detect a JSON to {mode} transition",
                                "source": {
                                    "experiment_id": source_id,
                                    "evidence_id": source_evidence["evidence_id"],
                                },
                                "response_reader": {"mode": "auto", "raw_only": True},
                                "termination": termination,
                                "execution_mode": "sync",
                                "deadline_ms": 10_000,
                            },
                        },
                    )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "completed")
                manifest = json.loads(
                    (root / response.json()["result"]["manifest_relative_path"]).read_text(
                        encoding="utf-8"
                    )
                )
                replay = manifest["replay"]
                self.assertFalse(replay["source_is_stream"])
                self.assertTrue(replay["stream_capture_enabled"])
                self.assertTrue(replay["response_is_stream"])
                self.assertEqual(replay["observed_response_mode"], mode)
                self.assertEqual(
                    replay["requested_replay_protocol"]["response_reader"]["mode"],
                    "auto",
                )
                self.assertEqual(
                    replay["replay_protocol"]["response_reader"]["mode"],
                    mode,
                )
                self.assertTrue(replay["replay_protocol"]["capture"]["stream"])
                self.assertTrue(
                    replay["replay_protocol"]["requirements"]["require_raw_capture"]
                )
                self.assertEqual(
                    manifest["stream_response_contract"]["status"],
                    "complete",
                )
                self.assertEqual(manifest["quality_summary"]["status"], "complete")
