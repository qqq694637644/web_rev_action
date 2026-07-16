from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from skill_temple.browser.adapters.contracts import (
    StreamCheckpoint,
    StreamWaitResult,
)
from skill_temple.browser_models import (
    CancelExperimentRequest,
    CaptureFlowRequest,
    FlowStep,
    OpenSessionRequest,
    RequestMatcher,
    WaitCondition,
)
from skill_temple.browser_service import (
    BrowserActionService,
    BrowserServiceError,
    Deadline,
    ExperimentStore,
)
from tests.browser.common import BrowserActionTestCase
from tests.fakes.browser import FakeJsReverse, FakePlaywright


class FinalizationBrowserTests(BrowserActionTestCase):
    def test_shared_browser_runtime_rejects_cross_session_queueing(self) -> None:
        class SlowPlaywright(FakePlaywright):
            def __init__(self, events: list[str]) -> None:
                super().__init__(events)
                self.active_steps = 0
                self.max_active_steps = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def execute_step(
                self,
                session_ref: str,
                step: FlowStep,
                experiment_dir: Path,
                deadline: Deadline,
            ) -> dict[str, Any]:
                self.active_steps += 1
                self.max_active_steps = max(
                    self.max_active_steps,
                    self.active_steps,
                )
                self.started.set()
                try:
                    await self.release.wait()
                    return await super().execute_step(
                        session_ref,
                        step,
                        experiment_dir,
                        deadline,
                    )
                finally:
                    self.active_steps -= 1

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            playwright = SlowPlaywright(events)
            js = FakeJsReverse(
                events,
                root,
                include_supporting_failure=False,
            )
            service = BrowserActionService(
                playwright=playwright,
                js_reverse=js,
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def scenario() -> str:
                for session_id in ["session_a", "session_b"]:
                    await service.run(
                        OpenSessionRequest(
                            operation="open_session",
                            payload={"session_id": session_id},
                        )
                    )
                requests = {
                    session_id: CaptureFlowRequest(
                        operation="capture_flow",
                        payload={
                            "session_id": session_id,
                            "objective": f"capture {session_id}",
                            "primary_request": {
                                "url_contains": "/api/resource",
                                "method": "POST",
                                "resource_types": ["fetch"],
                            },
                            "flow": [
                                {
                                    "step_id": f"click_{session_id}",
                                    "action": "click",
                                    "locator": {
                                        "role": "button",
                                        "name": "Send",
                                    },
                                }
                            ],
                            "wait_for": {
                                "type": "default_done_marker",
                                "request_matcher": {
                                    "url_contains": "/api/resource",
                                },
                            },
                            "execution_mode": "sync",
                            "deadline_ms": 15_000,
                        },
                    )
                    for session_id in ["session_a", "session_b"]
                }
                first = asyncio.create_task(service.run(requests["session_a"]))
                await playwright.started.wait()
                try:
                    with self.assertRaises(BrowserServiceError) as raised:
                        await service.run(requests["session_b"])
                    return raised.exception.code
                finally:
                    playwright.release.set()
                    await first
                    await service.close()

            self.assertEqual(asyncio.run(scenario()), "browser_busy")
            self.assertEqual(playwright.max_active_steps, 1)

    def test_task_cancellation_stops_capture_and_writes_interrupted_manifest(self) -> None:
        class BlockingPlaywright(FakePlaywright):
            def __init__(self, events: list[str]) -> None:
                super().__init__(events)
                self.started = asyncio.Event()

            async def execute_step(
                self,
                session_ref: str,
                step: FlowStep,
                experiment_dir: Path,
                deadline: Deadline,
            ) -> dict[str, Any]:
                self.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as exc:
                    exc.adapter_dispatch_started = True
                    raise
                raise AssertionError("unreachable")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            playwright = BlockingPlaywright(events)
            js = FakeJsReverse(
                events,
                root,
                include_supporting_failure=False,
            )
            service = BrowserActionService(
                playwright=playwright,
                js_reverse=js,
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def scenario() -> None:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "session_cancel"},
                    )
                )
                request = CaptureFlowRequest(
                    operation="capture_flow",
                    payload={
                        "session_id": "session_cancel",
                        "objective": "cancel during click",
                        "primary_request": {
                            "url_contains": "/api/resource",
                            "method": "POST",
                            "resource_types": ["fetch"],
                        },
                        "flow": [
                            {
                                "step_id": "blocking_click",
                                "action": "click",
                                "locator": {
                                    "role": "button",
                                    "name": "Send",
                                },
                            }
                        ],
                        "execution_mode": "sync",
                        "deadline_ms": 15_000,
                    },
                )
                task = asyncio.create_task(service.run(request))
                await playwright.started.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await service.close()

            asyncio.run(scenario())
            self.assertIn("js.stop", events)
            manifests = list((root / "experiments").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "interrupted")
            self.assertTrue(manifest["capture_health"]["collector_stopped"])
            self.assertEqual(
                manifest["steps"][0]["status"],
                "canceled_outcome_unknown",
            )
            self.assertEqual(
                manifest["capture_health"]["capture_namespace"],
                manifest["experiment_id"],
            )

    def test_stop_success_is_not_overwritten_by_post_stop_status_failure(self) -> None:
        class PostStopStatusFailureJs(FakeJsReverse):
            async def get_stream_status(
                self,
                capture_id: int,
                deadline: Deadline,
                **kwargs: Any,
            ) -> dict[str, Any]:
                if self.status_payload.get("capture", {}).get("status") == "stopped":
                    raise RuntimeError("synthetic post-stop status failure")
                return await super().get_stream_status(capture_id, deadline, **kwargs)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=PostStopStatusFailureJs(events, root, include_supporting_failure=False),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> dict[str, Any]:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "stop_status"},
                    )
                )
                request = CaptureFlowRequest(
                    operation="capture_flow",
                    payload={
                        "session_id": "stop_status",
                        "objective": "stop then status fails",
                        "primary_request": {
                            "url_contains": "/api/resource",
                            "expected_min_matches": 0,
                        },
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
            self.assertTrue(health["collector_stopped"])
            self.assertEqual(health["collector_cleanup"], "completed")
            self.assertIsNone(health["orphan_capture_id"])
            self.assertTrue(any("post-stop status" in item for item in manifest["warnings"]))

    def test_cancel_experiment_waits_for_cleanup_and_releases_browser(self) -> None:
        class BlockingPlaywright(FakePlaywright):
            def __init__(self, events: list[str]) -> None:
                super().__init__(events)
                self.started = asyncio.Event()

            async def execute_step(
                self,
                session_ref: str,
                step: FlowStep,
                experiment_dir: Path,
                deadline: Deadline,
            ) -> dict[str, Any]:
                self.started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            playwright = BlockingPlaywright(events)
            service = BrowserActionService(
                playwright=playwright,
                js_reverse=FakeJsReverse(events, root, include_supporting_failure=False),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> tuple[dict[str, Any], bool]:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "cancel_public"},
                    )
                )
                started = await service.run(
                    CaptureFlowRequest(
                        operation="capture_flow",
                        payload={
                            "session_id": "cancel_public",
                            "objective": "cancel public job",
                            "primary_request": {"expected_min_matches": 0},
                            "flow": [
                                {
                                    "step_id": "blocking_click",
                                    "action": "click",
                                    "locator": {"css": "#send"},
                                }
                            ],
                            "execution_mode": "job",
                            "job_timeout_ms": 30_000,
                        },
                    )
                )
                await playwright.started.wait()
                canceled = await service.run(
                    CancelExperimentRequest(
                        operation="cancel_experiment",
                        payload={
                            "experiment_id": started.experiment_id,
                            "session_id": "cancel_public",
                        },
                    )
                )
                manifest = service.experiments.load_manifest(started.experiment_id)
                released = service.coordinator.browser_owner is None
                self.assertEqual(canceled.status, "interrupted")
                await service.close()
                return manifest, released

            manifest, released = asyncio.run(exercise())
            self.assertTrue(released)
            self.assertEqual(manifest["status"], "interrupted")
            self.assertEqual(
                manifest["capture_health"]["collector_cleanup"],
                "completed",
            )

    def test_canceling_read_only_wait_step_records_canceled(self) -> None:
        class BlockingWaitJs(FakeJsReverse):
            def __init__(self, events: list[str], root: Path) -> None:
                super().__init__(events, root, include_supporting_failure=False)
                self.wait_started = asyncio.Event()

            async def wait_for_stream_condition(
                self,
                *,
                capture_id: int,
                request_matcher: RequestMatcher,
                condition: WaitCondition,
                checkpoint: StreamCheckpoint,
                deadline: Deadline,
            ) -> StreamWaitResult:
                self.wait_started.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            js = BlockingWaitJs(events, root)
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=js,
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> dict[str, Any]:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "cancel_wait"},
                    )
                )
                started = await service.run(
                    CaptureFlowRequest(
                        operation="capture_flow",
                        payload={
                            "session_id": "cancel_wait",
                            "objective": "cancel read-only wait",
                            "primary_request": {"expected_min_matches": 0},
                            "flow": [
                                {
                                    "step_id": "wait_event",
                                    "action": "wait",
                                    "condition": {
                                        "type": "first_event",
                                        "request_matcher": {"url_contains": "/api/resource"},
                                    },
                                }
                            ],
                            "execution_mode": "job",
                            "job_timeout_ms": 30_000,
                        },
                    )
                )
                await js.wait_started.wait()
                await service.run(
                    CancelExperimentRequest(
                        operation="cancel_experiment",
                        payload={
                            "experiment_id": started.experiment_id,
                            "session_id": "cancel_wait",
                        },
                    )
                )
                manifest = service.experiments.load_manifest(started.experiment_id)
                await service.close()
                return manifest

            manifest = asyncio.run(exercise())
            self.assertEqual(manifest["steps"][0]["status"], "canceled")
