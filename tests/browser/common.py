from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.browser_service import (
    BrowserActionService,
    ExperimentStore,
)
from tests.fakes.browser import FakeJsReverse, FakePlaywright


class BrowserActionTestCase(unittest.TestCase):
    def make_client(
        self,
        root: Path,
        *,
        fail_step: str | None = None,
        alignment_status: str = "aligned",
        include_supporting_failure: bool = True,
        primary_status: str = "finished",
        raw_capture_integrity: str = "complete",
        semantic_parse_integrity: str = "complete",
        request_snapshot_integrity: str = "complete",
        artifact_integrity: str = "complete",
        fail_stop: bool = False,
        post_alignment_status: str | None = None,
    ) -> tuple[TestClient, list[str], FakeJsReverse]:
        events: list[str] = []
        experiments = ExperimentStore(root)
        js = FakeJsReverse(
            events,
            experiments.root,
            alignment_status=alignment_status,
            include_supporting_failure=include_supporting_failure,
            primary_status=primary_status,
            raw_capture_integrity=raw_capture_integrity,
            semantic_parse_integrity=semantic_parse_integrity,
            request_snapshot_integrity=request_snapshot_integrity,
            artifact_integrity=artifact_integrity,
            fail_stop=fail_stop,
            post_alignment_status=post_alignment_status,
        )
        service = BrowserActionService(
            playwright=FakePlaywright(events, fail_step=fail_step),
            js_reverse=js,
            experiments=experiments,
            default_browser_endpoint="http://127.0.0.1:9222",
        )
        return TestClient(create_app(browser_service=service)), events, js

    @staticmethod
    def open_session(client: TestClient) -> None:
        response = client.post(
            "/v1/browser/run",
            json={
                "operation": "open_session",
                "payload": {
                    "session_id": "session_one",
                    "target": {"start_url": "https://example.test/app"},
                },
            },
        )
        assert response.status_code == 200, response.text

    @staticmethod
    def replay_response_analysis(manifest: dict[str, Any]) -> dict[str, Any]:
        replay_attempt = next(
            item for item in manifest["evidence"] if item.get("kind") == "replay_attempt"
        )
        analysis = replay_attempt.get("response_analysis")
        assert isinstance(analysis, dict)
        return analysis

    @staticmethod
    def capture_request(*, include_in_flight: bool = False) -> dict[str, Any]:
        return {
            "operation": "capture_flow",
            "payload": {
                "session_id": "session_one",
                "objective": "capture one resource stream",
                "target": {"expected_url_contains": "/app"},
                "primary_request": {
                    "url_contains": "/api/resource",
                    "method": "POST",
                    "resource_types": ["fetch"],
                    "expected_min_matches": 1,
                    "expected_max_matches": 1,
                    "allow_supporting_failures": True,
                    "include_in_flight": include_in_flight,
                },
                "flow": [
                    {
                        "step_id": "submit_resource",
                        "action": "fill",
                        "locator": {"placeholder": "Input"},
                        "value": "hello",
                    },
                    {
                        "step_id": "click_send",
                        "action": "click",
                        "locator": {"role": "button", "name": "Send"},
                    },
                ],
                "wait_for": {
                    "type": "default_done_marker",
                    "request_matcher": {
                        "url_contains": "/api/resource",
                        "method": "POST",
                    },
                },
                "deadline_ms": 10_000,
                "execution_mode": "sync",
            },
        }

    def capture_replay_source(
        self,
        client: TestClient,
        root: Path,
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        capture = self.capture_request()
        capture["payload"]["network_evidence"] = [
            {
                "selector_id": "resource_submit",
                "matcher": {
                    "url_contains": "/api/resource",
                    "method": "POST",
                },
                "export_parts": ["all"],
            }
        ]
        response = client.post("/v1/browser/run", json=capture)
        self.assertEqual(response.status_code, 200, response.text)
        experiment_id = response.json()["experiment_id"]
        manifest = json.loads(
            (root / "experiments" / experiment_id / "manifest.json").read_text(encoding="utf-8")
        )
        evidence = next(
            item for item in manifest["evidence"] if item.get("kind") == "network_request"
        )
        return experiment_id, evidence, manifest
