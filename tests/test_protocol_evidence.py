from __future__ import annotations

import json
import unittest

from pydantic import TypeAdapter, ValidationError

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
    ReplayRequestPayload,
    RequestMatcher,
    VolatileBinding,
)
from skill_temple.protocol_evidence import (
    aggregate_observation_completeness,
    assess_paired_mutation_effectiveness,
    binding_value_from_snapshot,
    build_network_observation,
    build_replay_spec,
    classify_replay_response,
    network_checkpoint,
    network_request_matches,
    public_network_summary,
    redacted_request_body_from_snapshot,
    request_shape_from_snapshot,
    requests_after_checkpoint,
    validate_binding_mutation_compatibility,
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
        self.assertEqual(
            observation["artifact_ids"],
            ["art_headers", "art_headers_extra", "art_network"],
        )
        self.assertNotIn("integrity_status", observation)

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

    def test_paired_mutation_effectiveness_requires_control_delta_and_equivalence(
        self,
    ) -> None:
        mutation = RemoveJsonPathMutation(
            type="remove_json_path",
            path="/messages/0/id",
        )
        binding = VolatileBinding(
            binding_id="parent",
            target="json_pointer",
            path="/parent_message_id",
            generator="uuid4",
            reuse_policy="fresh_equivalent",
        )
        control_spec, _ = build_replay_spec(
            self.snapshot(),
            [],
            volatile_bindings=[binding],
            binding_values={"parent": "control-parent"},
        )
        treatment_spec, _ = build_replay_spec(
            self.snapshot(),
            [mutation],
            volatile_bindings=[binding],
            binding_values={"parent": "treatment-parent"},
        )
        control_wire = self.snapshot()
        treatment_wire = self.snapshot()
        control_wire["requestBody"] = control_spec["body"]
        treatment_wire["requestBody"] = treatment_spec["body"]

        effective = assess_paired_mutation_effectiveness(
            mutation,
            control_wire,
            treatment_wire,
            volatile_bindings=[binding],
            control_binding_values={"parent": "control-parent"},
            treatment_binding_values={"parent": "treatment-parent"},
        )
        self.assertTrue(effective["target_delta_observed"])
        self.assertTrue(effective["non_target_fields_equivalent"])
        self.assertTrue(effective["volatile_bindings_effective"])
        self.assertTrue(effective["mutation_effective"])
        self.assertEqual(effective["control_wire_value"], "<identifier>")
        self.assertEqual(effective["treatment_wire_value"], "<absent>")

        unchanged = assess_paired_mutation_effectiveness(
            mutation,
            control_wire,
            control_wire,
            volatile_bindings=[binding],
            control_binding_values={"parent": "control-parent"},
            treatment_binding_values={"parent": "control-parent"},
        )
        self.assertFalse(unchanged["target_delta_observed"])
        self.assertFalse(unchanged["mutation_effective"])

        changed_body = json.loads(treatment_spec["body"]["text"])
        changed_body["model"] = "other-model"
        treatment_wire["requestBody"] = {
            **treatment_spec["body"],
            "text": json.dumps(changed_body),
        }
        non_target_change = assess_paired_mutation_effectiveness(
            mutation,
            control_wire,
            treatment_wire,
            volatile_bindings=[binding],
            control_binding_values={"parent": "control-parent"},
            treatment_binding_values={"parent": "treatment-parent"},
        )
        self.assertFalse(non_target_change["non_target_fields_equivalent"])
        self.assertFalse(non_target_change["mutation_effective"])

        target_binding = VolatileBinding(
            binding_id="message_id",
            target="json_pointer",
            path="/messages/0/id",
            generator="uuid4",
            reuse_policy="fresh_equivalent",
        )
        control_target_spec, _ = build_replay_spec(
            self.snapshot(),
            [],
            volatile_bindings=[target_binding],
            binding_values={"message_id": "control-id"},
        )
        treatment_target_spec, _ = build_replay_spec(
            self.snapshot(),
            [mutation],
            volatile_bindings=[target_binding],
            binding_values={"message_id": "treatment-id"},
        )
        control_target_wire = self.snapshot()
        treatment_target_wire = self.snapshot()
        control_target_wire["requestBody"] = control_target_spec["body"]
        treatment_target_wire["requestBody"] = treatment_target_spec["body"]
        target_binding_result = assess_paired_mutation_effectiveness(
            mutation,
            control_target_wire,
            treatment_target_wire,
            volatile_bindings=[target_binding],
            control_binding_values={"message_id": "control-id"},
            treatment_binding_values={"message_id": "treatment-id"},
        )
        self.assertTrue(target_binding_result["volatile_bindings_effective"])
        self.assertTrue(target_binding_result["mutation_effective"])

    def test_replay_mode_enforces_control_and_single_treatment_mutation(self) -> None:
        base = {
            "session_id": "session_one",
            "objective": "paired replay",
            "source_experiment_id": "exp_source",
            "source_evidence_id": "ev_source",
        }
        adapter = TypeAdapter(ReplayRequestPayload)
        control = adapter.validate_python({**base, "replay_mode": "control", "mutations": []})
        self.assertEqual(control.replay_mode, "control")

        with self.assertRaises(ValidationError):
            adapter.validate_python(
                {
                    **base,
                    "replay_mode": "control",
                    "mutations": [
                        {
                            "type": "remove_json_path",
                            "path": "/tracking_id",
                        }
                    ],
                }
            )
        treatment = adapter.validate_python(
            {
                "replay_mode": "treatment",
                "control_experiment_id": "exp_control",
                "mutation": {
                    "type": "remove_json_path",
                    "path": "/tracking_id",
                },
            }
        )
        self.assertEqual(treatment.control_experiment_id, "exp_control")
        with self.assertRaises(ValidationError):
            adapter.validate_python(
                {
                    "replay_mode": "treatment",
                    "control_experiment_id": "exp_control",
                    "mutation": {
                        "type": "remove_json_path",
                        "path": "/tracking_id",
                    },
                    "capture": {"stream": False},
                }
            )

    def test_response_classification_requires_field_validation_evidence(self) -> None:
        mutation = RemoveJsonPathMutation(
            type="remove_json_path",
            path="/messages/0/id",
        )
        validation = classify_replay_response(
            status=422,
            content_type="application/json",
            response_value={"missing": ["messages[0].id"]},
            mutation=mutation,
        )
        unknown = classify_replay_response(
            status=422,
            content_type="application/json",
            response_value={"error": "invalid request"},
            mutation=mutation,
        )
        auth = classify_replay_response(
            status=401,
            content_type="application/json",
            response_value={"error": "login required"},
            mutation=mutation,
        )
        rate = classify_replay_response(
            status=429,
            content_type="application/json",
            response_value={"error": "rate limited"},
            mutation=mutation,
        )
        server = classify_replay_response(
            status=502,
            content_type="text/html",
            response_value="bad gateway",
            mutation=mutation,
        )
        redirect = classify_replay_response(
            status=200,
            content_type="text/html",
            response_value="login",
            mutation=None,
            redirected=True,
            source_url="https://example.test/conversation",
            final_url="https://example.test/login",
            source_content_type="application/json",
        )
        content_mismatch = classify_replay_response(
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
        self.assertIn("field_required", validation["inference_hints"])
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
        false_positive = classify_replay_response(
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
        constrained = classify_replay_response(
            status=422,
            content_type="application/json",
            response_value={
                "field": "/model",
                "code": "invalid_enum",
            },
            mutation=replace_model,
        )
        conflict = classify_replay_response(
            status=409,
            content_type="application/json",
            response_value={
                "field": "/id",
                "code": "duplicate_id",
            },
            mutation=id_remove,
        )
        missing_content_type = classify_replay_response(
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
        self.assertIn("value_constraint", constrained["inference_hints"])
        self.assertEqual(conflict["classification"], "conflict")
        self.assertEqual(
            missing_content_type["classification"],
            "response_contract_mismatch",
        )

    def test_validation_paths_respect_json_and_query_case_but_not_header_case(self) -> None:
        json_case_mismatch = classify_replay_response(
            status=422,
            content_type="application/json",
            response_value={"field": "/messages/0/id", "code": "field_required"},
            mutation=RemoveJsonPathMutation(
                type="remove_json_path",
                path="/messages/0/ID",
            ),
        )
        query_case_mismatch = classify_replay_response(
            status=422,
            content_type="application/json",
            response_value={"field": "requestId", "code": "field_required"},
            mutation=RemoveQueryParameterMutation(
                type="remove_query_parameter",
                name="requestID",
            ),
        )
        header_case_match = classify_replay_response(
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
        result = classify_replay_response(
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

    def test_duplicate_header_and_query_values_use_ordered_multiplicity(self) -> None:
        control = self.snapshot()
        treatment = self.snapshot()
        control["requestHeadersArray"].extend(
            [
                {"name": "X-Debug", "value": "one"},
                {"name": "x-debug", "value": "two"},
            ]
        )
        treatment["requestHeadersArray"] = [
            item for item in treatment["requestHeadersArray"] if item["name"].lower() != "x-debug"
        ] + [{"name": "X-Debug", "value": "replacement"}]
        header_result = assess_paired_mutation_effectiveness(
            ReplaceHeaderMutation(
                type="replace_header",
                name="X-Debug",
                value="replacement",
            ),
            control,
            treatment,
            volatile_bindings=[],
            control_binding_values={},
            treatment_binding_values={},
        )

        query_control = self.snapshot()
        query_treatment = self.snapshot()
        query_control["url"] = "https://example.test/path?k=one&k=two&keep=yes"
        query_treatment["url"] = "https://example.test/path?keep=yes&k=replacement"
        query_result = assess_paired_mutation_effectiveness(
            ReplaceQueryParameterMutation(
                type="replace_query_parameter",
                name="k",
                value="replacement",
            ),
            query_control,
            query_treatment,
            volatile_bindings=[],
            control_binding_values={},
            treatment_binding_values={},
        )

        self.assertEqual(header_result["control_value_count"], 2)
        self.assertEqual(header_result["treatment_value_count"], 1)
        self.assertTrue(header_result["multiplicity_changed"])
        self.assertTrue(header_result["target_delta_observed"])
        self.assertEqual(query_result["control_wire_value"], ["<string>", "<string>"])
        self.assertEqual(query_result["treatment_value_count"], 1)
        self.assertTrue(query_result["target_delta_observed"])

    def test_http_304_is_inconclusive_not_success(self) -> None:
        result = classify_replay_response(
            status=304,
            content_type="application/json",
            response_value=None,
            mutation=RemoveJsonPathMutation(type="remove_json_path", path="/tracking_id"),
            source_content_type="application/json",
        )

        self.assertEqual(result["classification"], "redirect_or_cache_response")

    def test_non_json_body_is_part_of_non_target_equivalence(self) -> None:
        mutation = RemoveHeaderMutation(type="remove_header", name="X-Tracking")
        control = self.snapshot()
        treatment = self.snapshot()
        control["requestBody"] = {
            "available": True,
            "size": 5,
            "encoding": "utf8",
            "text": "alpha",
        }
        treatment["requestBody"] = {
            "available": True,
            "size": 4,
            "encoding": "utf8",
            "text": "beta",
        }
        treatment["requestHeadersArray"] = [
            item
            for item in treatment["requestHeadersArray"]
            if item["name"].lower() != "x-tracking"
        ]
        assessment = assess_paired_mutation_effectiveness(
            mutation,
            control,
            treatment,
            volatile_bindings=[],
            control_binding_values={},
            treatment_binding_values={},
        )

        self.assertTrue(assessment["target_delta_observed"])
        self.assertFalse(assessment["non_target_fields_equivalent"])
        self.assertFalse(assessment["mutation_effective"])

    def test_preserve_source_binding_and_pointer_overlap_rules(self) -> None:
        preserve = VolatileBinding(
            binding_id="parent",
            target="json_pointer",
            path="/parent_message_id",
            value_source="preserve_source",
            reuse_policy="same_value",
        )
        self.assertEqual(
            binding_value_from_snapshot(self.snapshot(), preserve),
            "parent-secret-id",
        )
        ancestor = VolatileBinding(
            binding_id="message",
            target="json_pointer",
            path="/messages/0",
            generator="uuid4",
        )
        with self.assertRaisesRegex(ValueError, "contains the mutation target"):
            validate_binding_mutation_compatibility(
                [ancestor],
                RemoveJsonPathMutation(
                    type="remove_json_path",
                    path="/messages/0/id",
                ),
            )
        descendant = VolatileBinding(
            binding_id="message_id",
            target="json_pointer",
            path="/messages/0/id",
            generator="uuid4",
        )
        validate_binding_mutation_compatibility(
            [descendant],
            RemoveJsonPathMutation(type="remove_json_path", path="/messages/0"),
        )

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

    def test_wire_order_changes_require_explicit_normalization(self) -> None:
        control = self.snapshot()
        treatment = json.loads(json.dumps(control))
        treatment_body = json.loads(treatment["requestBody"]["text"])
        treatment_body.pop("tracking_id")
        treatment["requestBody"]["text"] = json.dumps(treatment_body)
        treatment["requestHeadersArray"] = list(reversed(treatment["requestHeadersArray"]))
        treatment["url"] = "https://example.test/conversation?keep=yes&tracking=abc"
        mutation = RemoveJsonPathMutation(
            type="remove_json_path",
            path="/tracking_id",
        )

        strict = assess_paired_mutation_effectiveness(
            mutation,
            control,
            treatment,
            volatile_bindings=[],
            control_binding_values={},
            treatment_binding_values={},
        )
        normalized = assess_paired_mutation_effectiveness(
            mutation,
            control,
            treatment,
            volatile_bindings=[],
            control_binding_values={},
            treatment_binding_values={},
            normalize_wire_order=True,
        )

        self.assertFalse(strict["non_target_fields_equivalent"])
        self.assertTrue(normalized["non_target_fields_equivalent"])

    def test_exact_body_pointer_and_conflicting_validation_signals(self) -> None:
        mutation = RemoveJsonPathMutation(
            type="remove_json_path",
            path="/body/id",
        )
        result = classify_replay_response(
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
        wrong_target = classify_replay_response(
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
        binding = VolatileBinding(
            binding_id="second_token",
            target="header",
            name="X-Token",
            occurrence=1,
            value_source="preserve_source",
            reuse_policy="same_value",
        )

        self.assertEqual(binding_value_from_snapshot(snapshot, binding), "second")


if __name__ == "__main__":
    unittest.main()
