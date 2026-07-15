from __future__ import annotations

import json

from skill_temple.protocol_evidence import (
    aggregate_observation_completeness,
    build_network_observation,
    public_network_summary,
    redacted_request_body_from_snapshot,
    request_shape_from_snapshot,
)
from tests.protocol.common import ProtocolTestCase


class EvidenceProtocolTests(ProtocolTestCase):
    def test_network_observation_combines_sources_without_duplicate_verdicts(self) -> None:
        observation = build_network_observation(
            observation_id="obs_one",
            network_evidence={
                "evidence_id": "ev_network",
                "request_ids": {"reqid": 7, "network_request_id": "network-7"},
                "artifact_ids": ["art_network"],
                "artifact_paths": {"all": "network/request.json"},
                "summary": {
                    "url": "https://example.test/api/resource",
                    "method": "POST",
                    "status": 200,
                    "snapshot_integrity": {
                        "request_headers_completeness": "partial",
                        "request_body_completeness": "complete",
                        "response_headers_completeness": "complete",
                        "response_body_completeness": "complete",
                    },
                },
            },
            stream_request={
                "persistentRequestId": "persistent-7",
                "rawCaptureIntegrity": "complete",
                "semanticParseIntegrity": "partial",
                "artifactIntegrity": "complete",
                "coreArtifacts": [
                    {
                        "kind": "request_headers",
                        "writeStatus": "written",
                        "bytes": 10,
                        "artifactId": "art_headers",
                    },
                    {
                        "kind": "request_headers_extra",
                        "writeStatus": "written",
                        "bytes": 10,
                        "artifactId": "art_headers_extra",
                    },
                ],
            },
            association={"status": "matched", "method": "network_request_id"},
        )

        self.assertEqual(observation["association"]["confidence"], "exact")
        self.assertEqual(observation["completeness"]["request_headers"], "complete")
        self.assertEqual(observation["completeness"]["request_body"], "complete")
        self.assertEqual(observation["completeness"]["semantic_stream"], "partial")
        self.assertEqual(observation["facts"]["http_status"], 200)
        self.assertIsNone(observation["facts"]["request_lifecycle_status"])
        self.assertEqual(
            observation["artifact_ids"],
            ["art_headers", "art_headers_extra", "art_network"],
        )
        self.assertNotIn("integrity_status", observation)

    def test_stream_only_observation_keeps_lifecycle_separate_from_http_status(self) -> None:
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

        self.assertIsNone(observation["facts"]["http_status"])
        self.assertEqual(
            observation["facts"]["request_lifecycle_status"],
            "finished",
        )

    def test_observation_quality_aggregates_only_required_dimensions(self) -> None:
        observations = [
            {
                "completeness": {
                    "request_body": "complete",
                    "semantic_stream": "partial",
                    "response_body": "unknown",
                },
                "missing_evidence": ["semantic_stream", "response_body"],
            }
        ]

        dimensions, missing = aggregate_observation_completeness(
            observations,
            required_dimensions={"request_body", "semantic_stream"},
        )

        self.assertEqual(
            dimensions,
            {"request_body": "complete", "semantic_stream": "partial"},
        )
        self.assertEqual(missing, ["semantic_stream"])

    def test_public_network_summary_redacts_credentials_and_omits_bodies(self) -> None:
        summary = public_network_summary(self.snapshot())
        request_headers = {
            item["name"].lower(): item["value"] for item in summary["request_headers"]
        }
        response_headers = {
            item["name"].lower(): item["value"] for item in summary["response_headers"]
        }

        self.assertEqual(request_headers["authorization"], "<redacted>")
        self.assertEqual(request_headers["cookie"], "<redacted>")
        self.assertEqual(response_headers["set-cookie"], "<redacted>")
        self.assertNotIn("text", summary["request_body"])
        self.assertNotIn("text", summary["response_body"])
        self.assertIn("/records/0/id", summary["request_shape"]["paths"])
        self.assertEqual(
            summary["request_shape"]["paths"]["/records/0/id"]["value"],
            "<identifier>",
        )

    def test_request_shape_and_redacted_body_preserve_structure_without_values(self) -> None:
        shape = request_shape_from_snapshot(self.snapshot())
        redacted = redacted_request_body_from_snapshot(self.snapshot())

        self.assertEqual(shape["paths"]["/records"]["type"], "array")
        self.assertEqual(shape["paths"]["/records"]["length"], 1)
        self.assertEqual(shape["paths"]["/timezone_offset_min"]["value"], 480)
        self.assertEqual(redacted["records"][0]["id"], "<identifier>")
        self.assertEqual(redacted["records"][0]["content"]["segments"][0], "<string>")
        self.assertNotIn("record-secret-id", json.dumps(redacted))
        self.assertNotIn("fixture secret text", json.dumps(redacted))

    def test_request_shape_does_not_treat_arbitrary_id_suffix_as_identifier(self) -> None:
        snapshot = self.snapshot()
        snapshot["requestBody"]["text"] = json.dumps(
            {
                "id": "real-id",
                "record_id": "record-id",
                "requestId": "request-id",
                "valid": "yes",
                "grid": "dense",
                "hybrid": "mode",
                "solid": "state",
            }
        )
        shape = request_shape_from_snapshot(snapshot)

        self.assertEqual(shape["paths"]["/id"]["value"], "<identifier>")
        self.assertEqual(shape["paths"]["/record_id"]["value"], "<identifier>")
        self.assertEqual(shape["paths"]["/requestId"]["value"], "<identifier>")
        for path in ["/valid", "/grid", "/hybrid", "/solid"]:
            self.assertEqual(shape["paths"][path]["value"], "<string>")
