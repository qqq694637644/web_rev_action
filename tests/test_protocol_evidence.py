from __future__ import annotations

import json
import unittest

from skill_temple.browser_models import (
    RemoveHeaderMutation,
    RemoveJsonPathMutation,
    RemoveQueryParameterMutation,
    ReplaceJsonPathMutation,
    RequestMatcher,
)
from skill_temple.protocol_evidence import (
    build_replay_spec,
    network_checkpoint,
    network_request_matches,
    public_network_summary,
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
                        "required": "yes",
                        "optional": "keep",
                        "tracking": "abc",
                        "nested": {"value": 1},
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
            item["name"].lower(): item["value"]
            for item in summary["request_headers"]
        }
        response_headers = {
            item["name"].lower(): item["value"]
            for item in summary["response_headers"]
        }

        self.assertEqual(request_headers["authorization"], "<redacted>")
        self.assertEqual(request_headers["cookie"], "<redacted>")
        self.assertEqual(response_headers["set-cookie"], "<redacted>")
        self.assertNotIn("text", summary["request_body"])
        self.assertNotIn("text", summary["response_body"])

    def test_replay_mutations_drop_browser_managed_headers(
        self,
    ) -> None:
        spec, diff = build_replay_spec(
            self.snapshot(),
            [
                RemoveJsonPathMutation(type="remove_json_path", path="$.tracking"),
                ReplaceJsonPathMutation(
                    type="replace_json_path",
                    path="$.nested.value",
                    value=2,
                ),
                RemoveHeaderMutation(type="remove_header", name="X-Tracking"),
                RemoveQueryParameterMutation(
                    type="remove_query_parameter",
                    name="tracking",
                ),
            ],
        )

        header_names = {item["name"].lower() for item in spec["headers"]}
        body = json.loads(spec["body"]["text"])

        self.assertIn("authorization", header_names)
        self.assertNotIn("cookie", header_names)
        self.assertNotIn("x-tracking", header_names)
        self.assertNotIn("tracking=", spec["url"])
        self.assertEqual(body["nested"]["value"], 2)
        self.assertNotIn("tracking", body)
        self.assertNotIn("Bearer secret", json.dumps(diff))
        self.assertNotIn("session=secret", json.dumps(diff))

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


if __name__ == "__main__":
    unittest.main()
