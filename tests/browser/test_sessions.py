from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from skill_temple.browser.adapters.contracts import (
    PageState,
)
from skill_temple.browser_models import (
    CaptureFlowRequest,
    FlowStep,
    OpenSessionRequest,
)
from skill_temple.browser_service import (
    BrowserActionService,
    BrowserServiceError,
    Deadline,
    ExperimentStore,
)
from skill_temple.runtime_coordinator import RuntimeCoordinator, RuntimeOwner
from skill_temple.workspace_models import WorkspaceWriteFileRequest
from skill_temple.workspace_service import AnalysisWorkspaceService
from skill_temple.workspace_text_ops import WorkspaceToolError
from tests.browser.common import BrowserActionTestCase
from tests.fakes.browser import FakeJsReverse, FakePlaywright


class SessionsBrowserTests(BrowserActionTestCase):
    def test_same_session_rejects_a_second_background_job(self) -> None:
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
                js_reverse=FakeJsReverse(
                    events,
                    root,
                    include_supporting_failure=False,
                ),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def scenario() -> str:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "session_busy"},
                    )
                )
                request = CaptureFlowRequest(
                    operation="capture_flow",
                    payload={
                        "session_id": "session_busy",
                        "objective": "long running job",
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
                        "execution_mode": "job",
                        "job_timeout_ms": 30_000,
                    },
                )
                await service.run(request)
                await playwright.started.wait()
                try:
                    with self.assertRaises(BrowserServiceError) as raised:
                        await service.run(request)
                    return raised.exception.code
                finally:
                    await service.close()

            self.assertEqual(asyncio.run(scenario()), "session_busy")

    def test_service_shutdown_detaches_open_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, root),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> None:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "shutdown-open"},
                    )
                )
                await service.close()

            asyncio.run(exercise())
            self.assertIn("playwright.close", events)
            saved = json.loads(
                (root / "sessions" / "shutdown-open.json").read_text(encoding="utf-8")
            )
            self.assertEqual(saved["status"], "closed")
            self.assertEqual(saved["close_reason"], "service_shutdown")

    def test_runtime_coordinator_atomically_blocks_browser_and_workspace_operations(self) -> None:
        class BlockingOpenPlaywright(FakePlaywright):
            def __init__(self, events: list[str]) -> None:
                super().__init__(events)
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def open_session(
                self,
                session_ref: str,
                browser_endpoint: str,
                start_url: str | None,
                deadline: Deadline,
            ) -> PageState:
                self.started.set()
                await self.release.wait()
                return await super().open_session(
                    session_ref,
                    browser_endpoint,
                    start_url,
                    deadline,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            coordinator = RuntimeCoordinator()
            playwright = BlockingOpenPlaywright(events)
            service = BrowserActionService(
                playwright=playwright,
                js_reverse=FakeJsReverse(events, root),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
                coordinator=coordinator,
            )
            workspace = AnalysisWorkspaceService(root, coordinator=coordinator)

            async def exercise() -> tuple[str, str, int]:
                opening = asyncio.create_task(
                    service.run(
                        OpenSessionRequest(
                            operation="open_session",
                            payload={"session_id": "reserved_session"},
                        )
                    )
                )
                await playwright.started.wait()
                with self.assertRaises(BrowserServiceError) as browser_error:
                    await service.run(
                        CaptureFlowRequest(
                            operation="capture_flow",
                            payload={
                                "session_id": "other_session",
                                "objective": "must not queue behind open",
                                "primary_request": {"expected_min_matches": 0},
                                "execution_mode": "sync",
                                "deadline_ms": 10_000,
                            },
                        )
                    )
                with self.assertRaises(WorkspaceToolError) as workspace_error:
                    await workspace.write_file(
                        WorkspaceWriteFileRequest(
                            path="reports/during-open.md",
                            content="blocked\n",
                        )
                    )
                manifest_count = len(list((root / "experiments").glob("*/manifest.json")))
                playwright.release.set()
                await opening
                workspace_owner = RuntimeOwner(
                    kind="workspace",
                    owner_id="manual_workspace",
                    operation="workspaceExecPwsh",
                )
                await coordinator.reserve_workspace(workspace_owner)
                try:
                    with self.assertRaises(BrowserServiceError) as reverse_error:
                        await service.run(
                            OpenSessionRequest(
                                operation="open_session",
                                payload={"session_id": "blocked_by_workspace"},
                            )
                        )
                    reverse_code = reverse_error.exception.code
                finally:
                    await coordinator.release_workspace(workspace_owner.owner_id)
                await service.close()
                return (
                    browser_error.exception.code,
                    workspace_error.exception.code,
                    reverse_code,
                    manifest_count,
                )

            browser_code, workspace_code, reverse_code, manifest_count = asyncio.run(exercise())
            self.assertEqual(browser_code, "browser_busy")
            self.assertEqual(workspace_code, "browser_busy")
            self.assertEqual(reverse_code, "workspace_busy")
            self.assertEqual(manifest_count, 0)
