from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from tests.browser.common import BrowserActionTestCase


def _resolve_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    name = reference.rsplit("/", 1)[-1]
    return root["components"]["schemas"][name]


def _assert_no_composition(value: Any) -> None:
    if isinstance(value, dict):
        for forbidden in ["oneOf", "anyOf", "allOf", "discriminator"]:
            assert forbidden not in value
        for child in value.values():
            _assert_no_composition(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_composition(child)


class ContractsBrowserTests(BrowserActionTestCase):
    def test_openapi_exposes_only_the_stable_six_field_browser_envelopes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            schema = client.get("/openapi.json").json()

        run = schema["paths"]["/v1/browser/run"]["post"]
        inspect = schema["paths"]["/v1/browser/inspect"]["post"]
        self.assertIs(run["x-openai-isConsequential"], True)
        self.assertIs(inspect["x-openai-isConsequential"], False)

        for operation in [run, inspect]:
            request_schema = _resolve_schema(
                operation["requestBody"]["content"]["application/json"]["schema"],
                schema,
            )
            self.assertEqual(request_schema["type"], "object")
            self.assertEqual(
                set(request_schema["required"]),
                {
                    "contract_version",
                    "operation",
                    "payload_json",
                    "skill_id",
                    "skill_content_hash",
                    "operation_contract_hash",
                },
            )
            self.assertEqual(
                set(request_schema["properties"]),
                {
                    "contract_version",
                    "operation",
                    "payload_json",
                    "skill_id",
                    "skill_content_hash",
                    "operation_contract_hash",
                },
            )
            self.assertEqual(
                request_schema["properties"]["contract_version"]["const"],
                "2.0",
            )
            operation_schema = request_schema["properties"]["operation"]
            self.assertEqual(operation_schema["type"], "string")
            self.assertNotIn("enum", operation_schema)
            self.assertNotIn("$ref", operation_schema)
            self.assertEqual(request_schema["properties"]["payload_json"]["type"], "string")
            _assert_no_composition(request_schema)

        operation_ids = {
            item["operationId"]
            for path_item in schema["paths"].values()
            for item in path_item.values()
        }
        self.assertNotIn("retrieve" + "SkillContext", operation_ids)
        self.assertNotIn("search" + "SkillDocs", operation_ids)

    def test_strict_flow_schema_rejects_missing_locator_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                payload = self.request_payload(self.capture_request())
                payload["flow"] = [
                    {"step_id": "bad", "action": "click", "unknown": True}
                ]
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request("capture_flow", payload),
                )
            self.assertEqual(response.status_code, 422)
            body = response.json()["error"]
            self.assertEqual(body["code"], "invalid_operation_payload")
            self.assertFalse(body["dispatch_started"])
            self.assertTrue(body["issues"])

    def test_strict_flow_and_predicate_unions_reject_cross_action_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)

                click_payload = self.request_payload(self.capture_request())
                click_payload["flow"] = [
                    {
                        "step_id": "bad_click",
                        "action": "click",
                        "locator": {"role": "button", "name": "Send"},
                        "value": "not-allowed",
                    }
                ]
                click_response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request("capture_flow", click_payload),
                )

                fill_payload = self.request_payload(self.capture_request())
                fill_payload["flow"] = [
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
                    json=self.browser_request("capture_flow", fill_payload),
                )

                predicate_payload = self.request_payload(self.capture_request())
                predicate_payload["wait_for"] = {
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
                    json=self.browser_request("capture_flow", predicate_payload),
                )

            self.assertEqual(click_response.status_code, 422, click_response.text)
            self.assertEqual(fill_response.status_code, 422, fill_response.text)
            self.assertEqual(predicate_response.status_code, 422, predicate_response.text)

    def test_old_nested_payload_transport_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.browser_request("list_experiments", {})
            request.pop("payload_json")
            request["pay" + "load"] = {}
            response = client.post(
                "/v1/browser/inspect",
                json=request,
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "invalid_operation_payload")
        self.assertFalse(response.json()["error"]["dispatch_started"])

    def test_payload_json_is_a_string_not_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.browser_request("list_experiments", {})
            request["payload_json"] = {}
            response = client.post(
                "/v1/browser/inspect",
                json=request,
            )
        self.assertEqual(response.status_code, 422)
        self.assertIn("payload_json", json.dumps(response.json()))
