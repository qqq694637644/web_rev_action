from __future__ import annotations

import json
import tempfile
from pathlib import Path

from skill_temple.browser.adapters.contracts import (
    AlignmentResult,
    PageState,
)
from skill_temple.browser_service import (
    BrowserActionService,
)
from tests.browser.common import BrowserActionTestCase


class EvidenceBrowserTests(BrowserActionTestCase):
    def test_network_evidence_window_public_inspection_and_script_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            request = self.capture_request()
            payload = self.request_payload(request)
            payload["network_evidence"] = [
                {
                    "selector_id": "resource_submit",
                    "matcher": {
                        "url_contains": "/api/resource",
                        "method": "POST",
                        "resource_types": ["fetch"],
                    },
                    "max_matches": 2,
                    "export_parts": ["all"],
                    "include_initiator": True,
                }
            ]
            payload["series"] = {
                "analysis_series_id": "series_one",
                "scenario_type": "initial_record",
                "sequence_index": 1,
            }
            self.set_request_payload(request, payload)

            with client:
                self.open_session(client)
                captured = client.post("/v1/browser/run", json=request)
                self.assertEqual(captured.status_code, 200, captured.text)
                experiment_id = captured.json()["experiment_id"]

                listed = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "list_evidence",
                        {"experiment_id": experiment_id},
                    ),
                )
                self.assertEqual(listed.status_code, 200, listed.text)
                network_evidence = next(
                    item
                    for item in listed.json()["result"]["evidence"]
                    if item["kind"] == "network_request"
                )
                evidence_id_value = network_evidence["evidence_id"]

                network = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "get_network_evidence",
                        {
                            "experiment_id": experiment_id,
                            "evidence_id": evidence_id_value,
                        },
                    ),
                )
                initiator = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "get_request_initiator",
                        {
                            "experiment_id": experiment_id,
                            "evidence_id": evidence_id_value,
                        },
                    ),
                )
                console = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "list_console_errors",
                        {"experiment_id": experiment_id},
                    ),
                )
                scripts = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "search_scripts",
                        {
                            "session_id": "session_one",
                            "query": "buildResourceRequest",
                        },
                    ),
                )
                source = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "get_script_source",
                        {
                            "session_id": "session_one",
                            "url": "https://example.test/app.js",
                            "start_line": 10,
                            "end_line": 20,
                        },
                    ),
                )
                saved_source = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "save_script_source",
                        {
                            "session_id": "session_one",
                            "target_experiment_id": experiment_id,
                            "initiator_evidence_id": evidence_id_value,
                            "url": "https://example.test/app.js",
                            "start_line": 10,
                            "end_line": 20,
                            "evidence_label": "resource-builder",
                        },
                    ),
                )

            manifest = json.loads(
                (root / "experiments" / experiment_id / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["network_checkpoint"]["max_reqid"], 1)
            self.assertEqual(
                [item["reqid"] for item in manifest["network_summary"]["requests"]],
                [2],
            )
            self.assertEqual(network_evidence["request_ids"]["reqid"], 2)
            self.assertNotIn("Bearer secret", json.dumps(network_evidence))
            self.assertNotIn("session=secret", json.dumps(network_evidence))
            headers = {
                item["name"].lower(): item["value"]
                for item in network_evidence["summary"]["request_headers"]
            }
            self.assertEqual(headers["authorization"], "<redacted>")
            self.assertEqual(headers["cookie"], "<redacted>")
            all_artifact_id = next(
                item for item in network_evidence["artifact_ids"] if item.endswith("_all")
            )
            descriptor = next(
                item for item in manifest["artifacts"] if item["artifactId"] == all_artifact_id
            )
            self.assertEqual(descriptor["sensitivity"], "credential")
            self.assertTrue(descriptor["containsCredentials"])
            self.assertEqual(network.status_code, 200, network.text)
            self.assertNotIn("Bearer secret", network.text)
            self.assertEqual(initiator.status_code, 200, initiator.text)
            self.assertIn("app.js", initiator.text)
            self.assertEqual(console.status_code, 200, console.text)
            self.assertEqual(console.json()["result"]["count"], 1)
            self.assertIn("new error", console.text)
            self.assertEqual(scripts.status_code, 200, scripts.text)
            self.assertIn("buildResourceRequest", scripts.text)
            self.assertEqual(source.status_code, 200, source.text)
            self.assertIn("function buildResourceRequest", source.text)
            self.assertEqual(saved_source.status_code, 200, saved_source.text)
            saved_evidence = saved_source.json()["result"]["evidence"]
            self.assertEqual(saved_evidence["kind"], "script_source")
            self.assertEqual(saved_evidence["initiator_evidence_id"], evidence_id_value)
            self.assertEqual(len(saved_evidence["sha256"]), 64)
            saved_source_path = root / saved_evidence["artifact_paths"]["script_source"]
            self.assertIn(
                "function buildResourceRequest",
                saved_source_path.read_text(encoding="utf-8"),
            )
            evidence_kinds = {item["kind"] for item in manifest["evidence"]}
            self.assertIn("page_snapshot", evidence_kinds)
            self.assertIn("console_message", evidence_kinds)

    def test_request_context_hash_detects_cookie_value_change_without_storing_value(
        self,
    ) -> None:
        alignment = AlignmentResult(
            status="aligned",
            playwright_page=PageState(url="https://example.test/app"),
            js_reverse_page_id="page_one",
            js_reverse_page_url="https://example.test/app",
        )
        first = {
            "url": "https://example.test/api/resource",
            "requestHeadersCompleteness": "complete",
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=first; theme=dark"},
                {"name": "Authorization", "value": "Bearer first"},
            ],
        }
        second = {
            "url": "https://example.test/api/resource",
            "requestHeadersCompleteness": "complete",
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=second; theme=dark"},
                {"name": "Authorization", "value": "Bearer first"},
            ],
        }

        first_fp = BrowserActionService._environment_fingerprint(
            alignment,
            first,
            phase="pre_dispatch",
        )
        second_fp = BrowserActionService._environment_fingerprint(
            alignment,
            second,
            phase="pre_dispatch",
        )
        comparison = BrowserActionService._compare_environment_facts(
            first_fp,
            second_fp,
            ["request_context_sha256"],
        )
        exact_match = BrowserActionService._compare_environment_facts(
            first_fp,
            first_fp,
            ["request_context_sha256"],
        )

        self.assertNotEqual(
            first_fp["cookie_name_value_sha256"],
            second_fp["cookie_name_value_sha256"],
        )
        self.assertNotIn("session=first", json.dumps(first_fp))
        self.assertNotIn("Bearer first", json.dumps(first_fp))
        self.assertEqual(comparison["status"], "different")
        self.assertEqual(
            comparison["dimensions"]["request_context_sha256"]["status"],
            "different",
        )
        self.assertEqual(exact_match["status"], "equivalent")

        unavailable = BrowserActionService._environment_fingerprint(
            alignment,
            {
                "url": "https://example.test/api/resource",
                "requestHeadersArray": [{"name": "Cookie", "value": "session=first"}],
            },
            phase="pre_dispatch",
        )
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertIsNone(unavailable["request_context_sha256"])

        ordered_one = {
            "url": "https://example.test/api/resource",
            "requestHeadersCompleteness": "complete",
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=one; analytics=a"},
                {"name": "Cookie", "value": "session=two"},
                {"name": "X-Request-Nonce", "value": "nonce-one"},
            ],
        }
        ordered_two = {
            **ordered_one,
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=two"},
                {"name": "Cookie", "value": "session=one; analytics=b"},
                {"name": "X-Request-Nonce", "value": "nonce-two"},
            ],
        }
        ignored_only_change = {
            **ordered_one,
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=one; analytics=changed"},
                {"name": "Cookie", "value": "session=two"},
                {"name": "X-Request-Nonce", "value": "nonce-two"},
            ],
        }
        ordered_one_hash = BrowserActionService._request_context_hashes(ordered_one)
        ordered_two_hash = BrowserActionService._request_context_hashes(ordered_two)
        ignored_one_hash = BrowserActionService._request_context_hashes(
            ordered_one,
            ignored_cookie_names=["analytics"],
            ignored_context_headers=["x-request-nonce"],
        )
        ignored_two_hash = BrowserActionService._request_context_hashes(
            ordered_two,
            ignored_cookie_names=["analytics"],
            ignored_context_headers=["x-request-nonce"],
        )
        ignored_only_change_hash = BrowserActionService._request_context_hashes(
            ignored_only_change,
            ignored_cookie_names=["analytics"],
            ignored_context_headers=["x-request-nonce"],
        )
        self.assertNotEqual(
            ordered_one_hash["cookie_name_value_sha256"],
            ordered_two_hash["cookie_name_value_sha256"],
        )
        self.assertNotEqual(
            ordered_one_hash["request_context_sha256"],
            ordered_two_hash["request_context_sha256"],
        )
        self.assertNotEqual(
            ignored_one_hash["cookie_name_value_sha256"],
            ignored_two_hash["cookie_name_value_sha256"],
        )
        self.assertEqual(
            ignored_one_hash["request_context_sha256"],
            ignored_only_change_hash["request_context_sha256"],
        )
        self.assertEqual(
            ignored_one_hash["ignored_cookie_names"],
            ["analytics"],
        )
        self.assertEqual(
            ignored_one_hash["ignored_context_headers"],
            ["x-request-nonce"],
        )
