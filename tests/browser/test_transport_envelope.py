from __future__ import annotations

import tempfile
from pathlib import Path

from skill_temple.browser_service import BrowserServiceError
from tests.browser.common import BrowserActionTestCase


class BrowserTransportEnvelopeTests(BrowserActionTestCase):
    def test_valid_inspect_envelope_decodes_and_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            response = client.post(
                "/v1/browser/inspect",
                json=self.browser_request("list_experiments", {"limit": 10}),
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["operation"], "list_experiments")

    def test_valid_run_envelope_decodes_and_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "open_session",
                        {"session_id": "session_one", "target": {"page_index": 0}},
                    ),
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["operation"], "open_session")

    def assert_pre_dispatch_error(
        self,
        response: object,
        code: str,
        operation: str,
    ) -> None:
        assert hasattr(response, "status_code")
        body = response.json()["error"]  # type: ignore[attr-defined]
        self.assertEqual(body["code"], code)
        self.assertEqual(body["operation"], operation)
        self.assertFalse(body["dispatch_started"])
        self.assertTrue(body["suggested_next_action"])

    def test_malformed_duplicate_non_finite_and_non_object_json_are_rejected(self) -> None:
        cases = [
            ('{"limit":', "invalid_json"),
            ('{"limit":1,"limit":2}', "invalid_json"),
            ('{"limit":NaN}', "invalid_json"),
            ('{"limit":Infinity}', "invalid_json"),
            ('[]', "payload_must_be_object"),
            ('null', "payload_must_be_object"),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            for payload_json, code in cases:
                with self.subTest(payload_json=payload_json):
                    request = self.browser_request("list_experiments", {})
                    request["payload_json"] = payload_json
                    response = client.post(
                        "/v1/browser/inspect",
                        json=request,
                    )
                    self.assert_pre_dispatch_error(
                        response,
                        code,
                        "list_experiments",
                    )

    def test_oversized_payload_json_is_rejected_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.browser_request("list_experiments", {})
            request["payload_json"] = "{" + (" " * 262_144) + "}"
            response = client.post(
                "/v1/browser/inspect",
                json=request,
            )
        self.assertEqual(response.status_code, 422)
        body = response.json()["error"]
        self.assertEqual(body["code"], "invalid_operation_payload")
        self.assertFalse(body["dispatch_started"])

    def test_stale_skill_or_operation_hash_is_rejected_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, events, _ = self.make_client(Path(temp_dir))
            stale_skill = self.browser_request("open_session", {"session_id": "one"})
            stale_skill["skill_content_hash"] = "sha256:" + ("0" * 64)
            stale_contract = self.browser_request("open_session", {"session_id": "one"})
            stale_contract["operation_contract_hash"] = "sha256:" + ("1" * 64)
            skill_response = client.post("/v1/browser/run", json=stale_skill)
            contract_response = client.post("/v1/browser/run", json=stale_contract)
        for response in [skill_response, contract_response]:
            self.assertEqual(response.status_code, 409, response.text)
            body = response.json()["error"]
            self.assertEqual(body["code"], "stale_operation_contract")
            self.assertFalse(body["dispatch_started"])
            self.assertTrue(body["expected_contract_hash"].startswith("sha256:"))
            self.assertTrue(body["expected_skill_content_hash"].startswith("sha256:"))
        self.assertEqual(events, [])

    def test_unknown_and_cross_action_operations_are_rejected_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            unknown = client.post(
                "/v1/browser/inspect",
                json=self.browser_request("does_not_exist", {}),
            )
            cross = client.post(
                "/v1/browser/inspect",
                json=self.browser_request("open_session", {}),
            )
        self.assert_pre_dispatch_error(unknown, "unknown_operation", "does_not_exist")
        self.assert_pre_dispatch_error(cross, "unknown_operation", "open_session")

    def test_domain_validation_reports_json_pointer_issues(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            missing = client.post(
                "/v1/browser/inspect",
                json=self.browser_request("get_session", {}),
            )
            extra = client.post(
                "/v1/browser/inspect",
                json=self.browser_request(
                    "get_session",
                    {"session_id": "session_one", "unexpected": True},
                ),
            )
        for response in [missing, extra]:
            self.assert_pre_dispatch_error(
                response,
                "invalid_operation_payload",
                "get_session",
            )
            issues = response.json()["error"]["issues"]
            self.assertTrue(all(item["path"].startswith("/") for item in issues))

    def test_removed_baseline_alias_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            response = client.post(
                "/v1/browser/run",
                json=self.browser_request(
                    "capture" + "_baseline",
                    {"session_id": "session_one"},
                ),
            )
        self.assert_pre_dispatch_error(
            response,
            "unknown_operation",
            "capture" + "_baseline",
        )

    def test_unclassified_runtime_failure_is_not_rewritten_as_dispatched(self) -> None:
        def failing_service(
            exception_type: type[Exception],
        ) -> object:
            async def fail_before_dispatch(_request: object) -> object:
                raise exception_type("failure before dispatch")

            return fail_before_dispatch

        for error_type in (RuntimeError, OSError):
            with (
                self.subTest(error_type=error_type.__name__),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                client, _, _ = self.make_client(Path(temp_dir))
                service = client.app.state.browser_action_service
                service.run = failing_service(error_type)
                with self.assertRaisesRegex(error_type, "before dispatch"):
                    client.post(
                        "/v1/browser/run",
                        json=self.browser_request(
                            "open_session",
                            {"session_id": "session_one"},
                        ),
                    )

    def test_explicit_unknown_outcome_preserves_service_dispatch_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            service = client.app.state.browser_action_service

            async def fail_after_dispatch(_request: object) -> object:
                raise BrowserServiceError(
                    "operation_outcome_unknown",
                    "connection lost after browser dispatch",
                    502,
                    dispatch_started=True,
                    outcome="unknown",
                )

            service.run = fail_after_dispatch
            response = client.post(
                "/v1/browser/run",
                json=self.browser_request(
                    "open_session",
                    {"session_id": "session_one"},
                ),
            )
        self.assertEqual(response.status_code, 502, response.text)
        body = response.json()["error"]
        self.assertEqual(body["code"], "operation_outcome_unknown")
        self.assertTrue(body["dispatch_started"])
        self.assertEqual(body["outcome"], "unknown")
        self.assertIn("do not repeat", body["suggested_next_action"].lower())
