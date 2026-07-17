from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.browser.adapters.js_reverse import JsReverseMcpAdapter
from skill_temple.browser_service import BrowserActionService, Deadline, ExperimentStore
from tests.browser.common import BrowserActionTestCase
from tests.fakes.browser import FakePlaywright


class StreamStartValidationTests(BrowserActionTestCase):
    def test_invalid_stream_start_response_is_unknown_and_recovers_collector_handle(
        self,
    ) -> None:
        class InvalidStartTransport:
            def __init__(self, root: Path) -> None:
                self.root = root
                self.stop_calls: list[int] = []

            @property
            def generation(self) -> int:
                return 3

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name == "select_page":
                    if "listPageIdx" in arguments:
                        return {
                            "pages": [
                                {
                                    "pageIdx": 0,
                                    "pageId": "page-0",
                                    "url": "https://example.test/app",
                                    "selected": True,
                                }
                            ]
                        }
                    return {
                        "pages": [
                            {
                                "pageIdx": 0,
                                "pageId": "page-0",
                                "url": "https://example.test/app",
                                "selected": True,
                            }
                        ]
                    }
                if name == "start_stream_capture":
                    experiment_id = str(arguments["artifactNamespace"])
                    metadata = (
                        self.root
                        / "experiments"
                        / experiment_id
                        / "js-reverse"
                        / "capture-recovered"
                        / "capture.json"
                    )
                    metadata.parent.mkdir(parents=True, exist_ok=True)
                    metadata.write_text(
                        '{"captureId":41,"captureUuid":"11111111-1111-4111-8111-111111111111"}',
                        encoding="utf-8",
                    )
                    return {"capture": {"captureId": 0}}
                if name == "stop_stream_capture":
                    capture_id = int(arguments["captureId"])
                    self.stop_calls.append(capture_id)
                    return {
                        "capture": {
                            "captureId": capture_id,
                            "captureUuid": "11111111-1111-4111-8111-111111111111",
                            "status": "stopped",
                        }
                    }
                if name == "get_stream_status":
                    capture_id = int(arguments["captureId"])
                    return {
                        "capture": {
                            "captureId": capture_id,
                            "captureUuid": "11111111-1111-4111-8111-111111111111",
                            "status": "stopped",
                        },
                        "requests": [],
                        "pagination": {
                            "pageIdx": int(arguments.get("pageIdx", 0)),
                            "hasNextPage": False,
                            "totalPages": 1,
                        },
                    }
                raise AssertionError(name)

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            transport = InvalidStartTransport(root)
            service = BrowserActionService(
                playwright=FakePlaywright([]),
                js_reverse=JsReverseMcpAdapter(transport),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )
            client = TestClient(create_app(browser_service=service))
            with client:
                opened = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "open_session",
                        {"session_id": "invalid_stream_start"},
                    ),
                )
                response = client.post(
                    "/v1/browser/run",
                    json=self.browser_request(
                        "capture_flow",
                        {
                            "session_id": "invalid_stream_start",
                            "objective": "expose invalid stream start response",
                            "primary_request": {"expected_min_matches": 0},
                            "capture": {
                                "network": False,
                                "stream": True,
                                "trace": False,
                                "screenshots": False,
                                "page_snapshots": False,
                                "console_errors": False,
                            },
                            "requirements": {
                                "require_raw_capture": False,
                                "require_semantic_parse": False,
                                "require_request_snapshot": False,
                                "require_artifacts": False,
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    ),
                )
            self.assertEqual(opened.status_code, 200, opened.text)
            self.assertEqual(response.status_code, 502, response.text)
            error = response.json()["error"]
            self.assertEqual(error["code"], "operation_outcome_unknown")
            self.assertTrue(error["dispatch_started"])
            self.assertEqual(error["outcome"], "unknown")
            manifest = service.experiments.load_manifest(error["experiment_id"])
            self.assertEqual(manifest["stream_runtime"]["start_status"], "outcome_unknown")
            self.assertEqual(manifest["stream_runtime"]["capture_id"], 41)
            self.assertTrue(manifest["capture_health"]["collector_stopped"])
            self.assertEqual(manifest["capture_health"]["collector_cleanup"], "completed")
            self.assertEqual(transport.stop_calls, [41])
