from __future__ import annotations

import json
import unittest

from skill_temple.browser_models import (
    RemoveHeaderMutation,
    RemoveJsonPathMutation,
    ReplaceJsonPathMutation,
    ReplayRequestPayload,
    RequestMatcher,
)
from skill_temple.protocol_evidence import (
    assess_mutation_effectiveness,
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
            with self.subTest(name=name), self.assertRaisesRegex(
                ValueError,
                "browser-managed",
            ):
                RemoveHeaderMutation(type="remove_header", name=name)

    def test_mutation_effectiveness_uses_actual_wire_snapshot(self) -> None:
        mutation = RemoveJsonPathMutation(
            type="remove_json_path",
            path="/messages/0/id",
        )
        replay_spec, _ = build_replay_spec(self.snapshot(), [mutation])
        wire = self.snapshot()
        wire["requestBody"] = replay_spec["body"]
        effective = assess_mutation_effectiveness(mutation, wire)
        ineffective = assess_mutation_effectiveness(mutation, self.snapshot())

        self.assertTrue(effective["mutation_effective"])
        self.assertEqual(effective["mutation_observed_on_wire"], "<absent>")
        self.assertFalse(ineffective["mutation_effective"])

    def test_replay_mode_enforces_control_and_single_treatment_mutation(self) -> None:
        base = {
            "session_id": "session_one",
            "objective": "paired replay",
            "source_experiment_id": "exp_source",
            "source_evidence_id": "ev_source",
        }
        control = ReplayRequestPayload.model_validate(
            {**base, "replay_mode": "control", "mutations": []}
        )
        self.assertEqual(control.replay_mode, "control")

        with self.assertRaisesRegex(ValueError, "control replay requires mutations"):
            ReplayRequestPayload.model_validate(
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
        with self.assertRaisesRegex(ValueError, "control_experiment_id"):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "replay_mode": "treatment",
                    "mutations": [
                        {
                            "type": "remove_json_path",
                            "path": "/tracking_id",
                        }
                    ],
                }
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


if __name__ == "__main__":
    unittest.main()
