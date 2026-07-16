from __future__ import annotations

from skill_temple.browser_models import (
    RequestMatcher,
)
from skill_temple.protocol.matching import (
    network_checkpoint,
    network_request_matches,
    requests_after_checkpoint,
)
from tests.protocol.common import ProtocolTestCase


class MatchingProtocolTests(ProtocolTestCase):
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

    def test_network_matcher_uses_stable_reqid_url_method_resource_type_and_mime(self) -> None:
        request = {
            "reqid": 12,
            "url": "https://example.test/api/resource/123",
            "method": "POST",
            "resourceType": "fetch",
            "mimeType": "Application/JSON; charset=utf-8",
        }
        self.assertTrue(
            network_request_matches(
                request,
                RequestMatcher(
                    request_id="12",
                    url_contains="/api/resource/",
                    method="POST",
                    resource_types=["fetch"],
                    mime_types=["application/json"],
                ),
            )
        )
        self.assertFalse(
            network_request_matches(
                request,
                RequestMatcher(request_id="13"),
            )
        )
        self.assertFalse(
            network_request_matches(
                request,
                RequestMatcher(mime_types=["text/event-stream"]),
            )
        )
        self.assertFalse(
            network_request_matches(
                {key: value for key, value in request.items() if key != "mimeType"},
                RequestMatcher(mime_types=["application/json"]),
            )
        )
