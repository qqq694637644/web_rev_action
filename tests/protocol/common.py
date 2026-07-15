from __future__ import annotations

import json
import unittest


class ProtocolTestCase(unittest.TestCase):
    def snapshot(self) -> dict:
        return {
            "url": "https://example.test/api/resource?tracking=abc&keep=yes",
            "method": "POST",
            "resourceType": "fetch",
            "status": 200,
            "requestHeadersArray": [
                {"name": "Authorization", "value": "Bearer secret"},
                {"name": "Cookie", "value": "session=secret"},
                {"name": "Content-Type", "value": "application/json"},
                {"name": "X-Tracking", "value": "track-me"},
            ],
            "requestHeadersCompleteness": "complete",
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
                        "records": [
                            {
                                "id": "record-secret-id",
                                "source": {"kind": "client"},
                                "content": {"segments": ["fixture secret text"]},
                            }
                        ],
                        "cursor_id": "cursor-secret-id",
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
