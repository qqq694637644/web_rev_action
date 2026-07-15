from __future__ import annotations

from skill_temple.browser_models import (
    RemoveHeaderMutation,
    RemoveJsonPathMutation,
    RemoveQueryParameterMutation,
    ReplaceJsonPathMutation,
)
from skill_temple.protocol.analyzers.response import (
    analyze_replay_response,
)
from tests.protocol.common import ProtocolTestCase


class ResponseAnalyzersProtocolTests(ProtocolTestCase):
    def test_response_classification_requires_field_validation_evidence(self) -> None:
        mutation = RemoveJsonPathMutation(
            type="remove_json_path",
            path="/records/0/id",
        )
        validation = analyze_replay_response(
            status=422,
            content_type="application/json",
            response_value={"missing": ["records[0].id"]},
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
            source_url="https://example.test/api/resource",
            final_url="https://example.test/login",
            source_content_type="application/json",
        )
        content_mismatch = analyze_replay_response(
            status=200,
            content_type="text/html",
            response_value="login",
            mutation=None,
            source_url="https://example.test/api/resource",
            final_url="https://example.test/api/resource",
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
            response_value={"field": "/records/0/id", "code": "field_required"},
            mutation=RemoveJsonPathMutation(
                type="remove_json_path",
                path="/records/0/ID",
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

    def test_http_304_is_inconclusive_not_success(self) -> None:
        result = analyze_replay_response(
            status=304,
            content_type="application/json",
            response_value=None,
            mutation=RemoveJsonPathMutation(type="remove_json_path", path="/tracking_id"),
            source_content_type="application/json",
        )

        self.assertEqual(result["classification"], "redirect_or_cache_response")

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
