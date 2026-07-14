from __future__ import annotations

import json
import unittest

from pydantic import ValidationError

from skill_temple.browser_models import (
    AddHeaderMutation,
    AddJsonPathMutation,
    AddQueryParameterMutation,
    RemoveHeaderMutation,
    RemoveJsonPathMutation,
    RemoveQueryParameterMutation,
    ReplaceHeaderMutation,
    ReplaceJsonPathMutation,
    ReplaceQueryParameterMutation,
    ReplayBinding,
    ReplayRequestPayload,
    RequestMatcher,
)
from skill_temple.protocol_evidence import (
    aggregate_observation_completeness,
    analyze_replay_response,
    binding_value_from_snapshot,
    build_network_observation,
    build_replay_spec,
    network_checkpoint,
    network_request_matches,
    public_network_summary,
    redacted_request_body_from_snapshot,
    request_shape_from_snapshot,
    requests_after_checkpoint,
)


class ProtocolEvidenceTests(unittest.TestCase):
    def snapshot(self) -> dict:
        return {
            "url": "https://example.test/conversation?tracking=abc&keep=yes",
            "method": "POST",
            "resourceType": "fetch",
            "status": 200,
            "requestHeadersArray": [
                {"name": "Authorization", "value": "Bearer secret"},
                {"name": "Cookie", "value": "session=secret"},
                {"name": "Content-Type", "value": "application/json"},
                {"name": "X-Tracking", "value": "track-me"},
            ],
            "requestHeadersCompleteness": "complete",
            "responseHeadersArray": [
                {"name": "Set-Cookie", "value": "session=new-secret"},
                {"name": "Content-Type", "value": "application/json"},
            ],
            "requestBody": {
                "available": True,
                "encoding": "utf8",
                "size": 80,
                "text": json.dumps(
                    {
                        "messages": [
                            {
                                "id": "message-secret-id",
                                "author": {"role": "user"},
                                "content": {"parts": ["hello secret text"]},
                            }
                        ],
                        "parent_message_id": "parent-secret-id",
                        "model": "fixture-model",
                        "timezone_offset_min": 480,
                        "tracking_id": "tracking-secret-id",
                    }
                ),
            },
            "responseBody": {
                "available": True,
                "encoding": "utf8",
                "size": 11,
                "text": '{"ok":true}',
            },
        }

    def test_network_observation_combines_sources_without_duplicate_verdicts(self) -> None:
        observation = build_network_observation(
            observation_id="obs_one",
            network_evidence={
                "evidence_id": "ev_network",
                "request_ids": {"reqid": 7, "network_request_id": "network-7"},
                "artifact_ids": ["art_network"],
                "artifact_paths": {"all": "network/request.json"},
                "summary": {
                    "url": "https://example.test/conversation",
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

    def test_network_checkpoint_excludes_old_requests_and_optionally_includes_inflight(
        self,
    ) -> None:
        before = [
            {"reqid": 1, "pending": False},
            {"reqid": 2, "pending": True},
        ]
        after = [
            {"reqid": 1, "pending": False},
            {"reqid": 2, "pending": False},
            {"reqid": 3, "pending": False},
        ]
        checkpoint = network_checkpoint(before, generation=7)

        excluded = requests_after_checkpoint(
            after,
            checkpoint,
            include_in_flight=False,
        )
        included = requests_after_checkpoint(
            after,
            checkpoint,
            include_in_flight=True,
        )

        self.assertEqual([item["reqid"] for item in excluded], [3])
        self.assertEqual([item["reqid"] for item in included], [2, 3])
        self.assertEqual(checkpoint["collector_generation"], 7)

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
        self.assertIn("/messages/0/id", summary["request_shape"]["paths"])
        self.assertEqual(
            summary["request_shape"]["paths"]["/messages/0/id"]["value"],
            "<identifier>",
        )

    def test_request_shape_and_redacted_body_preserve_structure_without_values(self) -> None:
        shape = request_shape_from_snapshot(self.snapshot())
        redacted = redacted_request_body_from_snapshot(self.snapshot())

        self.assertEqual(shape["paths"]["/messages"]["type"], "array")
        self.assertEqual(shape["paths"]["/messages"]["length"], 1)
        self.assertEqual(shape["paths"]["/timezone_offset_min"]["value"], 480)
        self.assertEqual(redacted["messages"][0]["id"], "<identifier>")
        self.assertEqual(redacted["messages"][0]["content"]["parts"][0], "<text>")
        self.assertNotIn("message-secret-id", json.dumps(redacted))
        self.assertNotIn("hello secret text", json.dumps(redacted))

    def test_json_pointer_mutations_support_arrays(self) -> None:
        spec, diff = build_replay_spec(
            self.snapshot(),
            [
                ReplaceJsonPathMutation(
                    type="replace_json_path",
                    path="/messages/0/content/parts/0",
                    value="replacement text",
                ),
            ],
        )
        body = json.loads(spec["body"]["text"])

        self.assertEqual(body["messages"][0]["content"]["parts"][0], "replacement text")
        self.assertEqual(diff["mutations"][0]["value"], "<string>")

        removed, _ = build_replay_spec(
            self.snapshot(),
            [RemoveJsonPathMutation(type="remove_json_path", path="/messages/0/id")],
        )
        removed_body = json.loads(removed["body"]["text"])
        self.assertNotIn("id", removed_body["messages"][0])

        with self.assertRaisesRegex(ValueError, "out of range"):
            build_replay_spec(
                self.snapshot(),
                [
                    RemoveJsonPathMutation(
                        type="remove_json_path",
                        path="/messages/9/id",
                    )
                ],
            )

    def test_diff_keeps_source_headers_and_redacts_replacement_values(self) -> None:
        spec, diff = build_replay_spec(
            self.snapshot(),
            [RemoveHeaderMutation(type="remove_header", name="X-Tracking")],
        )
        source_headers = {name.lower() for name in diff["source"]["header_names"]}
        replay_headers = {name.lower() for name in diff["replay"]["header_names"]}

        self.assertIn("x-tracking", source_headers)
        self.assertNotIn("x-tracking", replay_headers)
        self.assertIn("authorization", replay_headers)
        self.assertNotIn("cookie", {item["name"].lower() for item in spec["headers"]})
        self.assertNotIn("Bearer secret", json.dumps(diff))
        self.assertNotIn("session=secret", json.dumps(diff))

    def test_browser_managed_header_mutations_are_rejected(self) -> None:
        for name in ["Cookie", "Origin", "Referer", "Content-Length", "Sec-Fetch-Site"]:
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(
                    ValueError,
                    "browser-managed",
                ),
            ):
                RemoveHeaderMutation(type="remove_header", name=name)

    def test_generic_replay_payload_rejects_legacy_modes_and_validates_inputs(self) -> None:
        base = {
            "session_id": "session_one",
            "objective": "generic replay",
            "source": {
                "experiment_id": "exp_source",
                "evidence_id": "ev_source",
            },
        }
        payload = ReplayRequestPayload.model_validate(
            {
                **base,
                "mutations": [
                    {"type": "remove_json_path", "path": "/tracking_id"},
                    {
                        "type": "add_json_path",
                        "path": "/feature",
                        "value": True,
                    },
                ],
                "extractors": [
                    {
                        "extractor_id": "created_id",
                        "type": "network_response_json",
                        "selector": {"url_contains": "/create", "method": "POST"},
                        "pointer": "/id",
                    }
                ],
                "bindings": [
                    {
                        "binding_id": "created_id",
                        "target": "json_pointer",
                        "path": "/parent_id",
                        "value_source": "extractor",
                        "extractor_id": "created_id",
                    },
                    {
                        "binding_id": "manual",
                        "target": "header",
                        "name": "X-Manual",
                        "value_source": "manual_input",
                        "value": "value",
                    },
                ],
                "comparison": {
                    "references": [
                        {
                            "experiment_id": "exp_reference",
                            "evidence_id": "ev_reference",
                        },
                        {
                            "experiment_id": "exp_other",
                            "observation_id": "obs_other",
                        },
                    ],
                    "dimensions": ["response_status", "environment"],
                    "environment": {
                        "preset": "explicit",
                        "dimensions": ["page_origin"],
                    },
                },
            }
        )
        self.assertEqual(len(payload.mutations), 2)
        self.assertEqual(
            [
                item.model_dump(mode="json", exclude_none=True)
                for item in payload.comparison.references
            ],
            [
                {"experiment_id": "exp_reference", "evidence_id": "ev_reference"},
                {"experiment_id": "exp_other", "observation_id": "obs_other"},
            ],
        )
        self.assertEqual(payload.comparison.environment.dimensions, ["page_origin"])

        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "comparison": {
                        "references": [
                            {
                                "experiment_id": "exp_reference",
                                "evidence_id": "ev_reference",
                            }
                        ],
                        "dimensions": ["environment"],
                    },
                }
            )
        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "comparison": {
                        "references": [
                            {
                                "experiment_id": "exp_reference",
                                "evidence_id": "ev_reference",
                                "observation_id": "obs_reference",
                            }
                        ],
                        "dimensions": ["response_status"],
                    },
                }
            )
        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "comparison": {
                        "references": [{"experiment_id": "exp_reference"}],
                        "dimensions": ["response_status"],
                    },
                }
            )
        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "comparison": {
                        "references": [
                            {
                                "experiment_id": "exp_reference",
                                "evidence_id": "ev_reference",
                            }
                        ],
                        "dimensions": ["environment"],
                        "environment": {
                            "preset": "explicit",
                            "dimensions": ["conversation_current_node"],
                        },
                    },
                }
            )
        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "termination": {
                        "conditions": [
                            {"type": "exact_sse_data", "value": "done-a"},
                            {"type": "exact_sse_data", "value": "done-b"},
                        ]
                    },
                }
            )
        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "termination": {
                        "conditions": [
                            {"type": "idle_window", "window_ms": 1_000},
                            {"type": "idle_window", "window_ms": 5_000},
                        ]
                    },
                }
            )
        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "termination": {
                        "conditions": [{"type": "text_pattern", "value": ""}]
                    },
                }
            )
        normalized_termination = ReplayRequestPayload.model_validate(
            {
                **base,
                "termination": {
                    "conditions": [
                        {
                            "type": "network_close",
                            "value": "ignored",
                            "event_name": "ignored",
                        },
                        {
                            "type": "idle_window",
                            "value": "ignored",
                            "event_name": "ignored",
                        },
                    ]
                },
            }
        )
        self.assertEqual(
            normalized_termination.termination.model_dump(
                mode="json",
                exclude_none=True,
            ),
            {
                "conditions": [
                    {"type": "network_close"},
                    {"type": "idle_window", "window_ms": 15_000},
                ]
            },
        )

        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "replay_mode": "control",
                }
            )
        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "bindings": [
                        {
                            "binding_id": "missing",
                            "target": "header",
                            "name": "X-Missing",
                            "value_source": "extractor",
                            "extractor_id": "not_declared",
                        }
                    ],
                }
            )

    def test_response_classification_requires_field_validation_evidence(self) -> None:
        mutation = RemoveJsonPathMutation(
            type="remove_json_path",
            path="/messages/0/id",
        )
        validation = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"missing": ["messages[0].id"]},
            mutation=mutation,
        )
        unknown = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"error": "invalid request"},
            mutation=mutation,
        )
        auth = analyze_replay_response(
            status=401,
            content_type="application/json",
            response_value={"error": "login required"},
            mutation=mutation,
        )
        rate = analyze_replay_response(
            status=429,
            content_type="application/json",
            response_value={"error": "rate limited"},
            mutation=mutation,
        )
        server = analyze_replay_response(
            status=502,
            content_type="text/html",
            response_value="bad gateway",
            mutation=mutation,
        )
        redirect = analyze_replay_response(
            status=200,
            content_type="text/html",
            response_value="login",
            mutation=None,
            redirected=True,
            source_url="https://example.test/conversation",
            final_url="https://example.test/login",
            source_content_type="application/json",
        )
        content_mismatch = analyze_replay_response(
            status=200,
            content_type="text/html",
            response_value="login",
            mutation=None,
            source_url="https://example.test/conversation",
            final_url="https://example.test/conversation",
            source_content_type="application/json",
        )

        self.assertEqual(validation["classification"], "validation_rejection")
        self.assertTrue(validation["observations"]["validation_like"])
        self.assertEqual(
            validation["analyzer"],
            {"name": "http_response_classifier", "version": "1"},
        )
        self.assertIn("field_required", validation["hints"])
        self.assertEqual(unknown["classification"], "unknown_rejection")
        self.assertEqual(auth["classification"], "authentication_failure")
        self.assertEqual(rate["classification"], "rate_limited")
        self.assertEqual(server["classification"], "server_failure")
        self.assertEqual(redirect["classification"], "unexpected_redirect")
        self.assertEqual(
            content_mismatch["classification"],
            "response_contract_mismatch",
        )

    def test_validation_matching_avoids_substring_and_distinguishes_constraints(self) -> None:
        id_remove = RemoveJsonPathMutation(type="remove_json_path", path="/id")
        false_positive = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"message": "invalid request"},
            mutation=id_remove,
        )
        replace_model = ReplaceJsonPathMutation(
            type="replace_json_path",
            path="/model",
            value="unsupported",
        )
        constrained = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={
                "field": "/model",
                "code": "invalid_enum",
            },
            mutation=replace_model,
        )
        conflict = analyze_replay_response(
            status=409,
            content_type="application/json",
            response_value={
                "field": "/id",
                "code": "duplicate_id",
            },
            mutation=id_remove,
        )
        missing_content_type = analyze_replay_response(
            status=204,
            content_type=None,
            response_value=None,
            mutation=None,
            source_content_type="text/event-stream",
        )

        self.assertEqual(false_positive["classification"], "unknown_rejection")
        self.assertEqual(
            false_positive["validation_evidence"]["strength"],
            "none",
        )
        self.assertEqual(constrained["classification"], "value_constraint")
        self.assertIn("value_constraint", constrained["hints"])
        self.assertEqual(conflict["classification"], "conflict")
        self.assertEqual(
            missing_content_type["classification"],
            "response_contract_mismatch",
        )

    def test_validation_paths_respect_json_and_query_case_but_not_header_case(self) -> None:
        json_case_mismatch = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"field": "/messages/0/id", "code": "field_required"},
            mutation=RemoveJsonPathMutation(
                type="remove_json_path",
                path="/messages/0/ID",
            ),
        )
        query_case_mismatch = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"field": "requestId", "code": "field_required"},
            mutation=RemoveQueryParameterMutation(
                type="remove_query_parameter",
                name="requestID",
            ),
        )
        header_case_match = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"field": "x-csrf-token", "code": "field_required"},
            mutation=RemoveHeaderMutation(
                type="remove_header",
                name="X-CSRF-Token",
            ),
        )

        self.assertEqual(json_case_mismatch["classification"], "unknown_rejection")
        self.assertEqual(query_case_mismatch["classification"], "unknown_rejection")
        self.assertEqual(header_case_match["classification"], "validation_rejection")

    def test_unrecognized_validation_codes_remain_inconclusive(self) -> None:
        result = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"field": "/id", "code": "not_required"},
            mutation=RemoveJsonPathMutation(type="remove_json_path", path="/id"),
        )

        self.assertEqual(result["classification"], "field_rejection")
        self.assertEqual(
            result["validation_evidence"]["semantic"],
            "not_required",
        )

    def test_request_shape_does_not_treat_arbitrary_id_suffix_as_identifier(self) -> None:
        snapshot = self.snapshot()
        snapshot["requestBody"]["text"] = json.dumps(
            {
                "id": "real-id",
                "message_id": "message-id",
                "requestId": "request-id",
                "valid": "yes",
                "grid": "dense",
                "hybrid": "mode",
                "solid": "state",
            }
        )
        shape = request_shape_from_snapshot(snapshot)

        self.assertEqual(shape["paths"]["/id"]["value"], "<identifier>")
        self.assertEqual(shape["paths"]["/message_id"]["value"], "<identifier>")
        self.assertEqual(shape["paths"]["/requestId"]["value"], "<identifier>")
        for path in ["/valid", "/grid", "/hybrid", "/solid"]:
            self.assertEqual(shape["paths"][path]["value"], "<string>")

    def test_http_304_is_inconclusive_not_success(self) -> None:
        result = analyze_replay_response(
            status=304,
            content_type="application/json",
            response_value=None,
            mutation=RemoveJsonPathMutation(type="remove_json_path", path="/tracking_id"),
            source_content_type="application/json",
        )

        self.assertEqual(result["classification"], "redirect_or_cache_response")

    def test_preserve_source_binding_and_mutation_order(self) -> None:
        preserve = ReplayBinding(
            binding_id="parent",
            target="json_pointer",
            path="/parent_message_id",
            value_source="preserve_source",
        )
        self.assertEqual(
            binding_value_from_snapshot(self.snapshot(), preserve),
            "parent-secret-id",
        )
        ancestor = ReplayBinding(
            binding_id="message",
            target="json_pointer",
            path="/messages/0",
            value_source="literal",
            value={
                "id": "bound-message-id",
                "author": {"role": "user"},
                "content": {"parts": ["bound text"]},
            },
        )
        spec, _ = build_replay_spec(
            self.snapshot(),
            [
                RemoveJsonPathMutation(
                    type="remove_json_path",
                    path="/messages/0/id",
                )
            ],
            bindings=[ancestor],
            binding_values={"message": ancestor.value},
        )
        body = json.loads(spec["body"]["text"])
        self.assertNotIn("id", body["messages"][0])
        self.assertEqual(body["messages"][0]["content"]["parts"], ["bound text"])

    def test_network_matcher_uses_stable_reqid_url_method_and_resource_type(self) -> None:
        request = {
            "reqid": 12,
            "url": "https://example.test/conversation/123",
            "method": "POST",
            "resourceType": "fetch",
        }
        self.assertTrue(
            network_request_matches(
                request,
                RequestMatcher(
                    request_id="12",
                    url_contains="/conversation/",
                    method="POST",
                    resource_types=["fetch"],
                ),
            )
        )
        self.assertFalse(
            network_request_matches(
                request,
                RequestMatcher(request_id="13"),
            )
        )

    def test_add_mutations_and_duplicate_occurrences_preserve_wire_order(self) -> None:
        snapshot = self.snapshot()
        snapshot["url"] = "https://example.test/path?tag=one&keep=x&tag=two"
        snapshot["requestHeadersArray"] = [
            {"name": "X-Tag", "value": "one"},
            {"name": "X-Keep", "value": "x"},
            {"name": "X-Tag", "value": "two"},
            {"name": "Content-Type", "value": "application/json"},
        ]
        spec, _ = build_replay_spec(
            snapshot,
            [
                AddJsonPathMutation(
                    type="add_json_path",
                    path="/new_field",
                    value="new",
                ),
                ReplaceHeaderMutation(
                    type="replace_header",
                    name="X-Tag",
                    value="changed",
                    occurrence=1,
                ),
                RemoveQueryParameterMutation(
                    type="remove_query_parameter",
                    name="tag",
                    occurrence=0,
                ),
                AddQueryParameterMutation(
                    type="add_query_parameter",
                    name="tag",
                    value="three",
                ),
                AddHeaderMutation(
                    type="add_header",
                    name="X-Tag",
                    value="three",
                ),
            ],
        )

        self.assertEqual(
            [(item["name"], item["value"]) for item in spec["headers"]],
            [
                ("X-Tag", "one"),
                ("X-Keep", "x"),
                ("X-Tag", "changed"),
                ("Content-Type", "application/json"),
                ("X-Tag", "three"),
            ],
        )
        self.assertEqual(
            spec["url"],
            "https://example.test/path?keep=x&tag=two&tag=three",
        )
        self.assertEqual(json.loads(spec["body"]["text"])["new_field"], "new")

    def test_query_mutation_preserves_non_target_raw_encoding_by_default(self) -> None:
        snapshot = self.snapshot()
        snapshot["url"] = (
            "https://example.test/path?untouched=a%20b&slash=%2f&tag=one&tag=two"
        )
        mutation = ReplaceQueryParameterMutation(
            type="replace_query_parameter",
            name="tag",
            value="changed",
            occurrence=1,
        )

        preserved, _ = build_replay_spec(snapshot, [mutation])
        normalized, _ = build_replay_spec(
            snapshot,
            [mutation],
            query_serialization="normalize",
        )

        self.assertEqual(
            preserved["url"],
            "https://example.test/path?untouched=a%20b&slash=%2f&tag=one&tag=changed",
        )
        self.assertEqual(preserved["querySerialization"], "preserve_raw")
        self.assertEqual(
            normalized["url"],
            "https://example.test/path?untouched=a+b&slash=%2F&tag=one&tag=changed",
        )
        self.assertEqual(normalized["querySerialization"], "normalize")

    def test_exact_body_pointer_and_conflicting_validation_signals(self) -> None:
        mutation = RemoveJsonPathMutation(
            type="remove_json_path",
            path="/body/id",
        )
        result = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={
                "errors": [
                    {"field": "/body/id", "code": "field_required"},
                    {"field": "/body/id", "code": "not_required"},
                ]
            },
            mutation=mutation,
        )
        wrong_target = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"field": "/body/id", "code": "field_required"},
            mutation=RemoveJsonPathMutation(type="remove_json_path", path="/id"),
        )

        self.assertTrue(result["observations"]["signals_conflict"])
        self.assertEqual(result["validation_evidence"]["semantic"], "conflicting")
        self.assertEqual(
            result["observations"]["normalized_validation_paths"],
            ["/body/id", "/body/id"],
        )
        self.assertEqual(wrong_target["classification"], "unknown_rejection")

    def test_preserve_source_binding_selects_duplicate_occurrence(self) -> None:
        snapshot = self.snapshot()
        snapshot["requestHeadersArray"].extend(
            [
                {"name": "X-Token", "value": "first"},
                {"name": "X-Token", "value": "second"},
            ]
        )
        binding = ReplayBinding(
            binding_id="second_token",
            target="header",
            name="X-Token",
            occurrence=1,
            value_source="preserve_source",
        )

        self.assertEqual(binding_value_from_snapshot(snapshot, binding), "second")


if __name__ == "__main__":
    unittest.main()
