from __future__ import annotations

import hashlib

from skill_temple.browser_service import BrowserActionService
from tests.browser.common import BrowserActionTestCase


class StreamAssociationBrowserTests(BrowserActionTestCase):
    def test_stream_network_fallback_uses_private_url_hash(self) -> None:
        raw_url = "https://example.test/api/resource?token=secret"
        network_entry = {
            "evidence_id": "ev-network",
            "request_url_sha256": hashlib.sha256(raw_url.encode("utf-8")).hexdigest(),
            "summary": {
                "url": "https://example.test/api/resource?token=<value>",
                "method": "POST",
            },
            "request_ids": {},
        }

        matched, association = BrowserActionService._associate_stream_network_evidence(
            {"url": raw_url, "method": "POST"},
            [network_entry],
        )

        self.assertIs(matched, network_entry)
        self.assertEqual(association["status"], "matched")
        self.assertEqual(association["method"], "url_method_fallback")

    def test_stream_association_prefers_stable_ids_and_fails_ambiguous_fallback(
        self,
    ) -> None:
        url = "https://example.test/api/resource"
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        entries = [
            {
                "evidence_id": "ev_one",
                "kind": "network_request",
                "request_url_sha256": url_hash,
                "request_ids": {
                    "network_request_id": "network-one",
                    "collector_generation": 7,
                    "cdp_request_id": "cdp-one",
                },
                "summary": {"url": url, "method": "POST"},
            },
            {
                "evidence_id": "ev_two",
                "kind": "network_request",
                "request_url_sha256": url_hash,
                "request_ids": {
                    "network_request_id": "network-two",
                    "collector_generation": 7,
                    "cdp_request_id": "cdp-two",
                },
                "summary": {"url": url, "method": "POST"},
            },
        ]
        matched, association = BrowserActionService._associate_stream_network_evidence(
            {
                "networkRequestId": "network-two",
                "collectorGeneration": 7,
                "cdpRequestId": "cdp-two",
                "url": url,
                "method": "POST",
            },
            entries,
        )
        ambiguous, fallback = BrowserActionService._associate_stream_network_evidence(
            {"url": url, "method": "POST"},
            entries,
        )

        self.assertEqual(matched["evidence_id"], "ev_two")
        self.assertEqual(
            association["method"],
            "network_request_id+cdp_request_id",
        )
        self.assertIsNone(ambiguous)
        self.assertEqual(fallback["status"], "ambiguous")
        self.assertEqual(fallback["candidate_count"], 2)

        duplicate_network_ids = [
            {
                "evidence_id": "ev_a",
                "request_ids": {
                    "network_request_id": "shared",
                    "cdp_request_id": "cdp-a",
                },
                "summary": {"url": url, "method": "POST"},
            },
            {
                "evidence_id": "ev_b",
                "request_ids": {
                    "network_request_id": "shared",
                    "cdp_request_id": "cdp-b",
                },
                "summary": {"url": url, "method": "POST"},
            },
        ]
        disambiguated, combined = BrowserActionService._associate_stream_network_evidence(
            {
                "networkRequestId": "shared",
                "cdpRequestId": "cdp-b",
                "url": url,
                "method": "POST",
            },
            duplicate_network_ids,
        )
        self.assertEqual(disambiguated["evidence_id"], "ev_b")
        self.assertEqual(combined["status"], "matched")
        self.assertEqual(
            combined["method"],
            "network_request_id+cdp_request_id",
        )
