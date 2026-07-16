from __future__ import annotations

import tempfile
from pathlib import Path

from tests.browser.common import BrowserActionTestCase


class ContractsBrowserTests(BrowserActionTestCase):
    def test_openapi_has_gpt_actions_compatible_browser_request_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            schema = client.get("/openapi.json").json()
        run = schema["paths"]["/v1/browser/run"]["post"]
        inspect = schema["paths"]["/v1/browser/inspect"]["post"]
        self.assertIs(run["x-openai-isConsequential"], True)
        self.assertIs(inspect["x-openai-isConsequential"], False)
        run_schema = run["requestBody"]["content"]["application/json"]["schema"]
        inspect_schema = inspect["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(run_schema["type"], "object")
        self.assertEqual(inspect_schema["type"], "object")
        self.assertEqual(set(run_schema["required"]), {"operation", "payload"})
        self.assertEqual(set(inspect_schema["required"]), {"operation", "payload"})
        self.assertEqual(
            set(run_schema["properties"]),
            {"contract_version", "operation", "payload", "skill_binding"},
        )
        self.assertEqual(
            set(inspect_schema["properties"]),
            {"contract_version", "operation", "payload"},
        )
        self.assertNotIn("oneOf", run_schema)
        self.assertNotIn("discriminator", run_schema)
        self.assertNotIn("oneOf", inspect_schema)
        self.assertNotIn("discriminator", inspect_schema)
        self.assertEqual(run_schema["properties"]["payload"]["type"], "object")
        self.assertEqual(inspect_schema["properties"]["payload"]["type"], "object")
        self.assertIn("oneOf", run_schema["properties"]["payload"])
        self.assertIn("oneOf", inspect_schema["properties"]["payload"])
        run_variants = str(run_schema["properties"]["payload"])
        inspect_variants = str(inspect_schema["properties"]["payload"])
        self.assertIn("CancelExperimentPayload", run_variants)
        self.assertIn("ReplayRequestPayload", run_variants)
        self.assertIn("SaveScriptSourcePayload", run_variants)
        self.assertIn("GetStreamStatusPayload", inspect_variants)
        for variant in [
            "ListEvidencePayload",
            "GetNetworkEvidencePayload",
            "GetRequestShapePayload",
            "GetRequestInitiatorPayload",
            "SearchScriptsPayload",
            "GetScriptSourcePayload",
            "ListConsoleErrorsPayload",
        ]:
            self.assertIn(variant, inspect_variants)
        status_payload = schema["components"]["schemas"]["GetStreamStatusPayload"]
        self.assertIn("experiment_id", status_payload["properties"])
        self.assertIn("capture_uuid", status_payload["properties"])
        self.assertNotIn("capture_id", status_payload["properties"])
        replay_payload = schema["components"]["schemas"]["ReplayRequestPayload"]
        for field in [
            "source",
            "mutations",
            "extractors",
            "bindings",
            "transport",
            "response_reader",
            "termination",
            "comparison",
            "query_serialization",
        ]:
            self.assertIn(field, replay_payload["properties"])
        self.assertNotIn("replay_mode", replay_payload["properties"])
        binding_payload = schema["components"]["schemas"]["ReplayBinding"]
        self.assertIn("value_source", binding_payload["properties"])
        self.assertIn("extractor_id", binding_payload["properties"])
        self.assertIn("value", binding_payload["properties"])
        self.assertNotIn("reuse_policy", binding_payload["properties"])
        reader = schema["components"]["schemas"]["ReplayResponseReader"]
        self.assertIn("mode", reader["properties"])
        self.assertIn("max_bytes", reader["properties"])
        self.assertIn("max_events", reader["properties"])
        self.assertNotIn("idle_timeout_ms", reader["properties"])
        self.assertIn("analyzer", reader["properties"])
        comparison = schema["components"]["schemas"]["ReplayComparison"]
        self.assertIn("references", comparison["properties"])
        self.assertIn("dimensions", comparison["properties"])
        reference = schema["components"]["schemas"]["ReplayComparisonReference"]
        self.assertIn("evidence_id", reference["properties"])
        self.assertIn("observation_id", reference["properties"])
        terminal = schema["components"]["schemas"]["ReplayTerminalCondition"]
        terminal_types = set(terminal["properties"]["type"]["enum"])
        self.assertIn("text_pattern", terminal_types)
        self.assertNotIn("byte_pattern", terminal_types)
        shape_payload = schema["components"]["schemas"]["GetRequestShapePayload"]
        for field in [
            "path_prefix",
            "page_idx",
            "page_size",
            "max_depth",
            "max_array_items",
            "include_redacted_body",
        ]:
            self.assertIn(field, shape_payload["properties"])

    def test_strict_flow_schema_rejects_missing_locator_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                request = self.capture_request()
                request["payload"]["flow"] = [
                    {"step_id": "bad", "action": "click", "unknown": True}
                ]
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 422)

    def test_strict_flow_and_predicate_unions_reject_cross_action_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                click_request = self.capture_request()
                click_request["payload"]["flow"] = [
                    {
                        "step_id": "bad_click",
                        "action": "click",
                        "locator": {"role": "button", "name": "Send"},
                        "value": "not-allowed",
                    }
                ]
                click_response = client.post(
                    "/v1/browser/run",
                    json=click_request,
                )
                fill_request = self.capture_request()
                fill_request["payload"]["flow"] = [
                    {
                        "step_id": "bad_fill",
                        "action": "fill",
                        "locator": {"placeholder": "Input"},
                        "value": "hello",
                        "intent": "stop_generation",
                    }
                ]
                fill_response = client.post(
                    "/v1/browser/run",
                    json=fill_request,
                )
                predicate_request = self.capture_request()
                predicate_request["payload"]["wait_for"] = {
                    "type": "event_predicate",
                    "request_matcher": {"url_contains": "/api/resource"},
                    "predicate": {
                        "type": "exact_data",
                        "value": "fixture-complete",
                        "path": "$.type",
                    },
                }
                predicate_response = client.post(
                    "/v1/browser/run",
                    json=predicate_request,
                )
            self.assertEqual(click_response.status_code, 422)
            self.assertEqual(fill_response.status_code, 422)
            self.assertEqual(predicate_response.status_code, 422)
