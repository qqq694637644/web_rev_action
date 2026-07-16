from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.browser.adapters.contracts import (
    McpToolCallError,
    StreamCheckpoint,
    StreamRequestCheckpoint,
    StreamWaitResult,
)
from skill_temple.browser.adapters.js_reverse import JsReverseMcpAdapter
from skill_temple.browser_models import (
    CaptureFlowRequest,
    ExactDataPredicate,
    OpenSessionRequest,
    RequestMatcher,
    WaitCondition,
)
from skill_temple.browser_service import (
    BrowserActionService,
    Deadline,
    ExperimentStore,
)
from skill_temple.runtime_coordinator import RuntimeCoordinator, RuntimeOwner
from tests.browser.common import BrowserActionTestCase
from tests.fakes.browser import FakeJsReverse, FakePlaywright


class StreamsBrowserTests(BrowserActionTestCase):
    def test_semantic_only_event_satisfies_first_event_after_checkpoint(self) -> None:
        class SemanticTransport:
            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "get_stream_status":
                    raise AssertionError(name)
                return {
                    "capture": {"captureId": 7, "version": 2},
                    "requests": [
                        {
                            "cdpRequestId": "req-semantic",
                            "url": "https://example.test/api/resource",
                            "method": "POST",
                            "resourceType": "eventsource",
                            "status": "streaming",
                            "rawEventCount": 0,
                            "semanticEventCount": 1,
                        }
                    ],
                }

            async def close(self) -> None:
                return None

        async def exercise() -> StreamWaitResult:
            adapter = JsReverseMcpAdapter(SemanticTransport())
            return await adapter.wait_for_stream_condition(
                capture_id=7,
                request_matcher=RequestMatcher(url_contains="/api/resource"),
                condition=WaitCondition(
                    type="first_event",
                    request_matcher=RequestMatcher(url_contains="/api/resource"),
                ),
                checkpoint=StreamCheckpoint(
                    version=1,
                    requests={
                        "req-semantic": StreamRequestCheckpoint(
                            status="streaming",
                            raw_event_index=-1,
                            semantic_event_index=-1,
                            primary_event_source="eventsource",
                        )
                    },
                ),
                deadline=Deadline(2_000),
            )

        result = asyncio.run(exercise())
        self.assertTrue(result.condition_met)
        self.assertEqual(
            result.checkpoint.requests["req-semantic"].semantic_event_index,
            0,
        )

    def test_old_done_event_cannot_satisfy_a_later_checkpoint(self) -> None:
        class CheckpointTransport:
            def __init__(self) -> None:
                self.status_calls = 0
                self.predicate_calls: list[dict[str, Any]] = []

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "get_stream_status":
                    raise AssertionError(name)
                if "eventPredicate" in arguments:
                    self.predicate_calls.append(arguments)
                    return {
                        "capture": {"captureId": 7, "version": 4},
                        "eventMatch": {
                            "matched": True,
                            "matchedEventIndex": 1,
                            "matchedRequestId": "req-checkpoint",
                            "matchedSource": "raw-stream",
                        },
                        "requests": [],
                    }
                self.status_calls += 1
                event_count = 1 if self.status_calls == 1 else 2
                return {
                    "capture": {
                        "captureId": 7,
                        "version": 3 if event_count == 1 else 4,
                    },
                    "requests": [
                        {
                            "cdpRequestId": "req-checkpoint",
                            "url": "https://example.test/api/resource",
                            "method": "POST",
                            "resourceType": "fetch",
                            "status": "streaming",
                            "rawEventCount": event_count,
                            "semanticEventCount": 0,
                        }
                    ],
                }

            async def close(self) -> None:
                return None

        transport = CheckpointTransport()

        async def exercise() -> StreamWaitResult:
            adapter = JsReverseMcpAdapter(transport)
            return await adapter.wait_for_stream_condition(
                capture_id=7,
                request_matcher=RequestMatcher(url_contains="/api/resource"),
                condition=WaitCondition(
                    type="event_predicate",
                    request_matcher=RequestMatcher(url_contains="/api/resource"),
                    predicate=ExactDataPredicate(
                        type="exact_data",
                        value="fixture-complete",
                    ),
                ),
                checkpoint=StreamCheckpoint(
                    version=2,
                    requests={
                        "req-checkpoint": StreamRequestCheckpoint(
                            status="streaming",
                            raw_event_index=0,
                            semantic_event_index=-1,
                            primary_event_source="raw-stream",
                        )
                    },
                ),
                deadline=Deadline(3_000),
            )

        result = asyncio.run(exercise())
        self.assertTrue(result.condition_met)
        self.assertGreaterEqual(transport.status_calls, 2)
        self.assertEqual(len(transport.predicate_calls), 1)
        self.assertEqual(
            transport.predicate_calls[0]["afterEventIndex"],
            0,
        )
        self.assertEqual(result.matched_event["matchedEventIndex"], 1)

    def test_stream_status_paginates_until_primary_request_is_found(self) -> None:
        class PaginatedTransport:
            def __init__(self) -> None:
                self.pages: list[int] = []

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "get_stream_status":
                    raise AssertionError(name)
                page_idx = int(arguments["pageIdx"])
                self.pages.append(page_idx)
                if page_idx == 0:
                    requests = [
                        {
                            "cdpRequestId": f"supporting-{index}",
                            "url": f"https://example.test/telemetry/{index}",
                            "method": "GET",
                            "resourceType": "fetch",
                            "status": "finished",
                            "rawEventCount": 0,
                            "semanticEventCount": 0,
                        }
                        for index in range(100)
                    ]
                    pagination = {
                        "pageIdx": 0,
                        "pageSize": 100,
                        "totalItems": 101,
                        "totalPages": 2,
                        "hasNextPage": True,
                        "hasPreviousPage": False,
                    }
                else:
                    requests = [
                        {
                            "cdpRequestId": "primary-request",
                            "url": "https://example.test/api/resource",
                            "method": "POST",
                            "resourceType": "fetch",
                            "status": "streaming",
                            "rawEventCount": 1,
                            "semanticEventCount": 0,
                        }
                    ]
                    pagination = {
                        "pageIdx": 1,
                        "pageSize": 100,
                        "totalItems": 101,
                        "totalPages": 2,
                        "hasNextPage": False,
                        "hasPreviousPage": True,
                    }
                return {
                    "capture": {"captureId": 7, "version": 2},
                    "requests": requests,
                    "pagination": pagination,
                }

            async def close(self) -> None:
                return None

        transport = PaginatedTransport()

        async def exercise() -> dict[str, Any]:
            adapter = JsReverseMcpAdapter(transport)
            return await adapter.get_stream_status(7, Deadline(2_000))

        payload = asyncio.run(exercise())
        self.assertEqual(transport.pages, [0, 1])
        self.assertEqual(len(payload["requests"]), 101)
        self.assertEqual(payload["requests"][-1]["cdpRequestId"], "primary-request")
        self.assertFalse(payload["pagination"]["hasNextPage"])

    def test_supporting_request_event_match_cannot_satisfy_primary_wait(self) -> None:
        class MismatchedEventTransport:
            def __init__(self) -> None:
                self.predicate_calls = 0

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "get_stream_status":
                    raise AssertionError(name)
                if "eventPredicate" in arguments:
                    self.predicate_calls += 1
                    matched_request = (
                        "supporting-request" if self.predicate_calls == 1 else "primary-request"
                    )
                    return {
                        "capture": {"captureId": 7, "version": 4},
                        "request": {
                            "cdpRequestId": "primary-request",
                            "url": "https://example.test/api/resource",
                            "method": "POST",
                            "resourceType": "fetch",
                            "status": "streaming",
                            "rawEventCount": 2,
                            "semanticEventCount": 0,
                        },
                        "eventMatch": {
                            "matched": True,
                            "matchedEventIndex": 1,
                            "matchedRequestId": matched_request,
                            "matchedSource": "raw-stream",
                        },
                    }
                return {
                    "capture": {"captureId": 7, "version": 4},
                    "requests": [
                        {
                            "cdpRequestId": "primary-request",
                            "url": "https://example.test/api/resource",
                            "method": "POST",
                            "resourceType": "fetch",
                            "status": "streaming",
                            "rawEventCount": 2,
                            "semanticEventCount": 0,
                        }
                    ],
                    "pagination": {
                        "pageIdx": 0,
                        "pageSize": 100,
                        "totalItems": 1,
                        "totalPages": 1,
                        "hasNextPage": False,
                        "hasPreviousPage": False,
                    },
                }

            async def close(self) -> None:
                return None

        transport = MismatchedEventTransport()

        async def exercise() -> StreamWaitResult:
            adapter = JsReverseMcpAdapter(transport)
            return await adapter.wait_for_stream_condition(
                capture_id=7,
                request_matcher=RequestMatcher(url_contains="/api/resource"),
                condition=WaitCondition(
                    type="event_predicate",
                    request_matcher=RequestMatcher(url_contains="/api/resource"),
                    predicate=ExactDataPredicate(
                        type="exact_data",
                        value="fixture-complete",
                    ),
                ),
                checkpoint=StreamCheckpoint(
                    version=2,
                    requests={
                        "primary-request": StreamRequestCheckpoint(
                            status="streaming",
                            raw_event_index=0,
                            semantic_event_index=-1,
                            primary_event_source="raw-stream",
                        )
                    },
                ),
                deadline=Deadline(3_000),
            )

        result = asyncio.run(exercise())
        self.assertTrue(result.condition_met)
        self.assertEqual(transport.predicate_calls, 2)
        self.assertEqual(
            result.matched_event["matchedRequestId"],
            "primary-request",
        )
        self.assertEqual(result.matched_request_ids, ["primary-request"])

    def test_terminal_wait_requires_a_post_checkpoint_transition(self) -> None:
        class TerminalTransport:
            def __init__(self) -> None:
                self.calls = 0

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "get_stream_status":
                    raise AssertionError(name)
                self.calls += 1
                new_status = "streaming" if self.calls == 1 else "finished"
                return {
                    "capture": {"captureId": 9, "version": 10 + self.calls},
                    "requests": [
                        {
                            "cdpRequestId": "old-finished",
                            "url": "https://example.test/api/resource/old",
                            "method": "POST",
                            "resourceType": "fetch",
                            "status": "finished",
                            "responseObserved": True,
                            "endedWallTimeMs": 100,
                            "rawEventCount": 1,
                            "semanticEventCount": 0,
                            "primaryEventSource": "raw-stream",
                        },
                        {
                            "cdpRequestId": "new-request",
                            "url": "https://example.test/api/resource/new",
                            "method": "POST",
                            "resourceType": "fetch",
                            "status": new_status,
                            "responseObserved": True,
                            "endedWallTimeMs": 200 if new_status == "finished" else None,
                            "rawEventCount": 1,
                            "semanticEventCount": 0,
                            "primaryEventSource": "raw-stream",
                        },
                    ],
                    "pagination": {"hasNextPage": False, "totalPages": 1},
                }

            async def close(self) -> None:
                return None

        async def exercise() -> StreamWaitResult:
            adapter = JsReverseMcpAdapter(TerminalTransport())
            return await adapter.wait_for_stream_condition(
                capture_id=9,
                request_matcher=RequestMatcher(url_contains="/api/resource"),
                condition=WaitCondition(
                    type="network_finished",
                    request_matcher=RequestMatcher(url_contains="/api/resource"),
                ),
                checkpoint=StreamCheckpoint(
                    version=10,
                    requests={
                        "old-finished": StreamRequestCheckpoint(
                            response_observed=True,
                            status="finished",
                            terminal_wall_time_ms=100,
                            raw_event_index=0,
                        ),
                        "new-request": StreamRequestCheckpoint(
                            response_observed=True,
                            status="streaming",
                            raw_event_index=0,
                        ),
                    },
                ),
                deadline=Deadline(3_000),
            )

        result = asyncio.run(exercise())
        self.assertEqual(result.matched_request_ids, ["new-request"])
        self.assertEqual(result.terminal_status, "finished")

    def test_semantic_cursor_advances_independently_from_raw_cursor(self) -> None:
        class DualCursorTransport:
            def __init__(self) -> None:
                self.predicate_arguments: dict[str, Any] | None = None

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "get_stream_status":
                    raise AssertionError(name)
                request = {
                    "cdpRequestId": "dual-request",
                    "url": "https://example.test/api/resource",
                    "method": "GET",
                    "resourceType": "eventsource",
                    "status": "streaming",
                    "responseObserved": True,
                    "rawEventCount": 100,
                    "semanticEventCount": 11,
                    "primaryEventSource": "raw-stream",
                }
                if "eventPredicate" in arguments:
                    self.predicate_arguments = dict(arguments)
                    return {
                        "capture": {"captureId": 10, "version": 22},
                        "request": request,
                        "eventMatch": {
                            "matched": True,
                            "matchedEventIndex": 10,
                            "matchedRequestId": "dual-request",
                            "matchedSource": "eventsource",
                        },
                    }
                return {
                    "capture": {"captureId": 10, "version": 22},
                    "requests": [request],
                    "pagination": {"hasNextPage": False, "totalPages": 1},
                }

            async def close(self) -> None:
                return None

        transport = DualCursorTransport()

        async def exercise() -> StreamWaitResult:
            adapter = JsReverseMcpAdapter(transport)
            return await adapter.wait_for_stream_condition(
                capture_id=10,
                request_matcher=RequestMatcher(url_contains="/api/resource"),
                condition=WaitCondition(
                    type="event_predicate",
                    request_matcher=RequestMatcher(url_contains="/api/resource"),
                    predicate=ExactDataPredicate(type="exact_data", value="semantic-new"),
                ),
                checkpoint=StreamCheckpoint(
                    version=21,
                    requests={
                        "dual-request": StreamRequestCheckpoint(
                            response_observed=True,
                            status="streaming",
                            raw_event_index=99,
                            semantic_event_index=9,
                            primary_event_source="raw-stream",
                        )
                    },
                ),
                deadline=Deadline(3_000),
            )

        result = asyncio.run(exercise())
        self.assertTrue(result.condition_met)
        self.assertEqual(result.matched_request_ids, ["dual-request"])
        self.assertEqual(transport.predicate_arguments["eventSource"], "eventsource")
        self.assertEqual(transport.predicate_arguments["afterEventIndex"], 9)

    def test_network_request_summary_paginates_past_one_hundred(self) -> None:
        class NetworkTransport:
            def __init__(self) -> None:
                self.pages: list[int] = []

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                if name != "list_network_requests":
                    raise AssertionError(name)
                page = int(arguments["pageIdx"])
                self.pages.append(page)
                if page == 0:
                    requests = [{"reqid": index} for index in range(100)]
                    has_next = True
                else:
                    requests = [{"reqid": 100}]
                    has_next = False
                return {
                    "requests": requests,
                    "pagination": {
                        "hasNextPage": has_next,
                        "totalPages": 2,
                    },
                }

            async def close(self) -> None:
                return None

        transport = NetworkTransport()

        async def exercise() -> dict[str, Any]:
            adapter = JsReverseMcpAdapter(transport)
            return await adapter.list_network_requests(RequestMatcher(), Deadline(2_000))

        result = asyncio.run(exercise())
        self.assertEqual(transport.pages, [0, 1])
        self.assertEqual(len(result["requests"]), 101)

    def test_terminal_stream_status_is_resolved_by_experiment_and_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir), include_supporting_failure=False)
            with client:
                self.open_session(client)
                captured = client.post(
                    "/v1/browser/run",
                    json=self.capture_request(),
                )
                self.assertEqual(captured.status_code, 200, captured.text)
                experiment_id = captured.json()["experiment_id"]
                status = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "get_stream_status",
                        {
                            "experiment_id": experiment_id,
                            "capture_uuid": "11111111-1111-4111-8111-111111111111",
                        },
                    ),
                )
                mismatch = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "get_stream_status",
                        {
                            "experiment_id": experiment_id,
                            "capture_uuid": "22222222-2222-4222-8222-222222222222",
                        },
                    ),
                )
            self.assertEqual(status.status_code, 200, status.text)
            self.assertEqual(status.json()["result"]["source"], "manifest")
            self.assertEqual(
                status.json()["result"]["stream"]["capture"]["captureUuid"],
                "11111111-1111-4111-8111-111111111111",
            )
            self.assertEqual(mismatch.status_code, 409)

    def test_live_stream_status_requires_matching_transport_generation(self) -> None:
        class GenerationJs(FakeJsReverse):
            def __init__(self, events: list[str], root: Path) -> None:
                super().__init__(events, root, include_supporting_failure=False)
                self.generation = 5
                self.status_calls = 0

            @property
            def transport_generation(self) -> int:
                return self.generation

            async def get_stream_status(
                self,
                capture_id: int,
                deadline: Deadline,
                **kwargs: Any,
            ) -> dict[str, Any]:
                self.status_calls += 1
                return {
                    "capture": {
                        "captureId": capture_id,
                        "captureUuid": "55555555-5555-4555-8555-555555555555",
                        "status": "capturing",
                    },
                    "requests": [],
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            coordinator = RuntimeCoordinator()
            experiments = ExperimentStore(root)
            js = GenerationJs(events, root)
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=js,
                experiments=experiments,
                default_browser_endpoint="http://127.0.0.1:9222",
                coordinator=coordinator,
            )
            experiment_id, _, manifest = experiments.create_experiment(
                session_id="live_status",
                operation="capture_flow",
                objective="live status identity",
                deadline=Deadline(10_000),
                experiment_id="exp_live_status",
            )
            manifest["stream_runtime"] = {
                "start_status": "confirmed",
                "capture_id": 9,
                "capture_uuid": "55555555-5555-4555-8555-555555555555",
                "transport_generation": 5,
            }
            manifest["stream_status"] = {
                "capture": {
                    "captureUuid": "55555555-5555-4555-8555-555555555555",
                    "status": "persisted",
                }
            }
            experiments.write_manifest(experiment_id, manifest)

            async def reserve() -> None:
                await coordinator.reserve_browser(
                    RuntimeOwner(
                        kind="browser",
                        owner_id=experiment_id,
                        operation="capture_flow",
                        session_id="live_status",
                        experiment_id=experiment_id,
                    )
                )

            asyncio.run(reserve())
            client = TestClient(create_app(browser_service=service))
            with client:
                live = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "get_stream_status",
                        {
                            "experiment_id": experiment_id,
                            "capture_uuid": "55555555-5555-4555-8555-555555555555",
                        },
                    ),
                )
                js.generation = 6
                persisted = client.post(
                    "/v1/browser/inspect",
                    json=self.browser_request(
                        "get_stream_status",
                        {"experiment_id": experiment_id},
                    ),
                )
            self.assertEqual(live.status_code, 200, live.text)
            self.assertEqual(live.json()["result"]["source"], "live-mcp")
            self.assertEqual(persisted.status_code, 200, persisted.text)
            self.assertEqual(persisted.json()["result"]["source"], "manifest")
            self.assertEqual(js.status_calls, 1)

    def test_unknown_stream_start_is_not_reported_as_stopped(self) -> None:
        class UnknownStartJs(FakeJsReverse):
            @property
            def transport_generation(self) -> int:
                return 7

            async def start_stream_capture(
                self,
                *,
                experiment_id: str,
                matcher: RequestMatcher,
                include_in_flight: bool,
                deadline: Deadline,
            ) -> dict[str, Any]:
                capture_dir = (
                    self.evidence_root
                    / "experiments"
                    / experiment_id
                    / "js-reverse"
                    / "capture-unknown"
                )
                capture_dir.mkdir(parents=True, exist_ok=True)
                (capture_dir / "capture.json").write_text(
                    json.dumps(
                        {
                            "captureId": 77,
                            "captureUuid": "33333333-3333-4333-8333-333333333333",
                            "status": "capturing",
                        }
                    ),
                    encoding="utf-8",
                )
                raise McpToolCallError(
                    "start outcome unknown",
                    outcome_unknown=True,
                    transport_generation=7,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=UnknownStartJs(events, root),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> dict[str, Any]:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "unknown_start"},
                    )
                )
                request = CaptureFlowRequest(
                    operation="capture_flow",
                    payload={
                        "session_id": "unknown_start",
                        "objective": "unknown start",
                        "primary_request": {"expected_min_matches": 0},
                        "execution_mode": "sync",
                        "deadline_ms": 10_000,
                    },
                )
                response = await service.run(request)
                manifest = service.experiments.load_manifest(response.experiment_id)
                await service.close()
                return manifest

            manifest = asyncio.run(exercise())
            health = manifest["capture_health"]
            self.assertEqual(health["stream_start_status"], "outcome_unknown")
            self.assertFalse(health["collector_stopped"])
            self.assertEqual(health["collector_cleanup"], "unknown")
            self.assertEqual(
                health["capture_uuid"],
                "33333333-3333-4333-8333-333333333333",
            )
            self.assertEqual(health["capture_namespace"], manifest["experiment_id"])

    def test_stream_disabled_has_complete_empty_quality_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.browser_request(
                "capture_flow",
                {
                    "session_id": "session_one",
                    "objective": "page-only baseline",
                    "primary_request": {
                        "expected_min_matches": 0,
                        "expected_max_matches": 1,
                        "allow_supporting_failures": False,
                    },
                    "capture": {
                        "stream": False,
                        "network": False,
                        "trace": False,
                        "screenshots": False,
                    },
                    "execution_mode": "sync",
                    "deadline_ms": 10_000,
                },
            )
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "completed")
            manifest = json.loads(
                (Path(temp_dir) / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["quality_summary"]["status"], "complete")
            self.assertEqual(manifest["quality_summary"]["observation_count"], 0)
            self.assertNotIn("collector_integrity", manifest)

    def test_stream_association_prefers_stable_ids_and_fails_ambiguous_fallback(
        self,
    ) -> None:
        entries = [
            {
                "evidence_id": "ev_one",
                "kind": "network_request",
                "request_ids": {
                    "network_request_id": "network-one",
                    "collector_generation": 7,
                    "cdp_request_id": "cdp-one",
                },
                "summary": {"url": "https://example.test/api/resource", "method": "POST"},
            },
            {
                "evidence_id": "ev_two",
                "kind": "network_request",
                "request_ids": {
                    "network_request_id": "network-two",
                    "collector_generation": 7,
                    "cdp_request_id": "cdp-two",
                },
                "summary": {"url": "https://example.test/api/resource", "method": "POST"},
            },
        ]
        matched, association = BrowserActionService._associate_stream_network_evidence(
            {
                "networkRequestId": "network-two",
                "collectorGeneration": 7,
                "cdpRequestId": "cdp-two",
                "url": "https://example.test/api/resource",
                "method": "POST",
            },
            entries,
        )
        ambiguous, fallback = BrowserActionService._associate_stream_network_evidence(
            {
                "url": "https://example.test/api/resource",
                "method": "POST",
            },
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
                "summary": {
                    "url": "https://example.test/api/resource",
                    "method": "POST",
                },
            },
            {
                "evidence_id": "ev_b",
                "request_ids": {
                    "network_request_id": "shared",
                    "cdp_request_id": "cdp-b",
                },
                "summary": {
                    "url": "https://example.test/api/resource",
                    "method": "POST",
                },
            },
        ]
        disambiguated, combined = BrowserActionService._associate_stream_network_evidence(
            {
                "networkRequestId": "shared",
                "cdpRequestId": "cdp-b",
                "url": "https://example.test/api/resource",
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

    def test_network_snapshot_does_not_upgrade_stream_artifact_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                include_supporting_failure=False,
                artifact_integrity="failed",
            )
            capture = self.capture_request()
            payload = self.request_payload(capture)
            payload["network_evidence"] = [
                {
                    "selector_id": "resource_submit",
                    "matcher": {
                        "url_contains": "/api/resource",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            self.set_request_payload(capture, payload)
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=capture)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "failed")
            manifest = json.loads(
                (
                    root / "experiments" / response.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            stream = next(item for item in manifest["evidence"] if item["kind"] == "stream_request")
            self.assertEqual(
                stream["summary"]["stream_artifact_integrity"],
                "failed",
            )
            self.assertNotIn("network_snapshot_integrity", stream["summary"])
            self.assertEqual(
                manifest["network_observations"][0]["completeness"]["stream_artifacts"],
                "failed",
            )

    def test_stop_sequence_accepts_finished_or_timeout_observation(self) -> None:
        for condition in [
            {
                "type": "network_finished",
                "request_matcher": {"url_contains": "/api/resource"},
            },
            {"type": "timeout", "timeout_ms": 1_000},
        ]:
            request = CaptureFlowRequest(
                operation="capture_flow",
                payload={
                    "session_id": "session_one",
                    "objective": "observe Stop without assuming cancel",
                    "primary_request": {"expected_min_matches": 0},
                    "flow": [
                        {
                            "step_id": "wait_started",
                            "action": "wait",
                            "condition": {
                                "type": "first_event",
                                "request_matcher": {"url_contains": "/api/resource"},
                            },
                        },
                        {
                            "step_id": "stop",
                            "action": "click",
                            "locator": {"css": "#stop"},
                            "intent": "stop_generation",
                        },
                        {
                            "step_id": "observe",
                            "action": "wait",
                            "condition": condition,
                        },
                    ],
                },
            )
            with self.subTest(condition=condition["type"]):
                self.assertEqual(request.payload.flow[-1].condition.type, condition["type"])
