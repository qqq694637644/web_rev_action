from __future__ import annotations

import json

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
)
from skill_temple.protocol_evidence import (
    binding_value_from_snapshot,
    build_replay_spec,
)
from tests.protocol.common import ProtocolTestCase


class MutationsProtocolTests(ProtocolTestCase):
    def test_json_pointer_mutations_support_arrays(self) -> None:
        spec, diff = build_replay_spec(
            self.snapshot(),
            [
                ReplaceJsonPathMutation(
                    type="replace_json_path",
                    path="/records/0/content/segments/0",
                    value="replacement text",
                ),
            ],
        )
        body = json.loads(spec["body"]["text"])

        self.assertEqual(body["records"][0]["content"]["segments"][0], "replacement text")
        self.assertEqual(diff["mutations"][0]["value"], "<string>")

        removed, _ = build_replay_spec(
            self.snapshot(),
            [RemoveJsonPathMutation(type="remove_json_path", path="/records/0/id")],
        )
        removed_body = json.loads(removed["body"]["text"])
        self.assertNotIn("id", removed_body["records"][0])

        with self.assertRaisesRegex(ValueError, "out of range"):
            build_replay_spec(
                self.snapshot(),
                [
                    RemoveJsonPathMutation(
                        type="remove_json_path",
                        path="/records/9/id",
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
                            "dimensions": ["unsupported_dimension"],
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
        empty_termination = ReplayRequestPayload.model_validate(
            {
                **base,
                "termination": {"conditions": []},
            }
        )
        self.assertEqual(
            empty_termination.termination.model_dump(mode="json", exclude_none=True),
            {"conditions": [{"type": "network_close"}]},
        )

        invalid_occurrence_payloads = [
            {
                "mutations": [
                    {
                        "type": "replace_header",
                        "name": "X-Test",
                        "value": "value",
                        "occurrence": -1,
                    }
                ]
            },
            {
                "mutations": [
                    {
                        "type": "replace_query_parameter",
                        "name": "item",
                        "value": "value",
                        "occurrence": -1,
                    }
                ]
            },
            {
                "mutations": [
                    {
                        "type": "add_header",
                        "name": "X-Test",
                        "value": "value",
                        "occurrence": 0,
                    }
                ]
            },
            {
                "mutations": [
                    {
                        "type": "add_query_parameter",
                        "name": "item",
                        "value": "value",
                        "occurrence": 0,
                    }
                ]
            },
            {
                "extractors": [
                    {
                        "extractor_id": "negative",
                        "type": "network_response_json",
                        "selector": {"url_contains": "/create", "method": "POST"},
                        "pointer": "/id",
                        "occurrence": -1,
                    }
                ]
            },
        ]
        for invalid in invalid_occurrence_payloads:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ReplayRequestPayload.model_validate({**base, **invalid})

        with self.assertRaises(ValidationError):
            ReplayRequestPayload.model_validate(
                {
                    **base,
                    "network_evidence": [
                        {
                            "selector_id": "replay_request",
                            "matcher": {"url_contains": "/supporting"},
                        }
                    ],
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

    def test_preserve_source_binding_and_mutation_order(self) -> None:
        preserve = ReplayBinding(
            binding_id="cursor",
            target="json_pointer",
            path="/cursor_id",
            value_source="preserve_source",
        )
        self.assertEqual(
            binding_value_from_snapshot(self.snapshot(), preserve),
            "cursor-secret-id",
        )
        ancestor = ReplayBinding(
            binding_id="record",
            target="json_pointer",
            path="/records/0",
            value_source="literal",
            value={
                "id": "bound-record-id",
                "source": {"kind": "client"},
                "content": {"segments": ["bound text"]},
            },
        )
        spec, _ = build_replay_spec(
            self.snapshot(),
            [
                RemoveJsonPathMutation(
                    type="remove_json_path",
                    path="/records/0/id",
                )
            ],
            bindings=[ancestor],
            binding_values={"record": ancestor.value},
        )
        body = json.loads(spec["body"]["text"])
        self.assertNotIn("id", body["records"][0])
        self.assertEqual(body["records"][0]["content"]["segments"], ["bound text"])

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
