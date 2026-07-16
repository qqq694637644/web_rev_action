from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from skill_temple.browser.adapters.contracts import (
    AdapterError,
    PageState,
)
from skill_temple.browser_models import (
    CaptureFlowRequest,
    CloseSessionRequest,
    FlowStep,
    GetSessionRequest,
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
    def test_open_unknown_persists_provisional_session_and_context(self) -> None:
        class UnknownOpenPlaywright(FakePlaywright):
            async def open_session(
                self,
                session_ref: str,
                browser_endpoint: str,
                start_url: str | None,
                deadline: Deadline,
            ) -> PageState:
                raise AdapterError(
                    "attach transport disconnected",
                    dispatch_started=True,
                    outcome_unknown=True,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            service = BrowserActionService(
                playwright=UnknownOpenPlaywright(events),
                js_reverse=FakeJsReverse(events, root),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> tuple[BrowserServiceError, dict[str, Any], dict[str, Any]]:
                with self.assertRaises(BrowserServiceError) as raised:
                    await service.run(
                        OpenSessionRequest(
                            operation="open_session",
                            payload={"session_id": "open_unknown"},
                        )
                    )
                inspected = await service.inspect(
                    GetSessionRequest(
                        operation="get_session",
                        payload={"session_id": "open_unknown"},
                    )
                )
                saved = service.experiments.load_session("open_unknown")
                assert saved is not None
                return raised.exception, inspected.result["session"], saved

            error, inspected, saved = asyncio.run(exercise())
            self.assertEqual(error.code, "operation_outcome_unknown")
            self.assertEqual(error.session_id, "open_unknown")
            self.assertEqual(inspected["status"], "open_outcome_unknown")
            self.assertEqual(saved["status"], "open_outcome_unknown")

    def test_confirmed_attach_with_alignment_failure_is_inspectable_as_unaligned(self) -> None:
        class FailedAlignJs(FakeJsReverse):
            async def align_page(
                self,
                page: PageState,
                deadline: Deadline,
                page_id: str | None = None,
            ) -> Any:
                raise AdapterError(
                    "alignment transport failed",
                    dispatch_started=True,
                    outcome_unknown=False,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FailedAlignJs(events, root),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> tuple[BrowserServiceError, dict[str, Any]]:
                with self.assertRaises(BrowserServiceError) as raised:
                    await service.run(
                        OpenSessionRequest(
                            operation="open_session",
                            payload={"session_id": "open_unaligned"},
                        )
                    )
                saved = service.experiments.load_session("open_unaligned")
                assert saved is not None
                return raised.exception, saved

            error, saved = asyncio.run(exercise())
            self.assertEqual(error.code, "browser_adapter_failed")
            self.assertEqual(error.session_id, "open_unaligned")
            self.assertEqual(saved["status"], "open_unaligned")
            self.assertEqual(saved["playwright_page_url"], "https://example.test/app")
            self.assertEqual(saved["attach_outcome"], "confirmed")
            self.assertEqual(saved["alignment_outcome"], "failed")

    def test_confirmed_attach_with_unknown_alignment_keeps_both_facts(self) -> None:
        class UnknownAlignJs(FakeJsReverse):
            async def align_page(
                self,
                page: PageState,
                deadline: Deadline,
                page_id: str | None = None,
            ) -> Any:
                raise AdapterError(
                    "alignment transport disconnected",
                    dispatch_started=True,
                    outcome_unknown=True,
                )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=UnknownAlignJs(events, root),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> tuple[BrowserServiceError, dict[str, Any]]:
                with self.assertRaises(BrowserServiceError) as raised:
                    await service.run(
                        OpenSessionRequest(
                            operation="open_session",
                            payload={"session_id": "alignment_unknown"},
                        )
                    )
                saved = service.experiments.load_session("alignment_unknown")
                assert saved is not None
                return raised.exception, saved

            error, saved = asyncio.run(exercise())
            self.assertEqual(error.code, "operation_outcome_unknown")
            self.assertEqual(saved["status"], "open_unaligned")
            self.assertEqual(saved["attach_outcome"], "confirmed")
            self.assertEqual(saved["alignment_outcome"], "unknown")

    def test_close_unknown_persists_non_open_state_and_context(self) -> None:
        class UnknownClosePlaywright(FakePlaywright):
            fail_close = False

            async def close_session(self, session_ref: str, deadline: Deadline) -> None:
                if self.fail_close:
                    raise AdapterError(
                        "detach transport disconnected",
                        dispatch_started=True,
                        outcome_unknown=True,
                    )
                await super().close_session(session_ref, deadline)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            playwright = UnknownClosePlaywright(events)
            service = BrowserActionService(
                playwright=playwright,
                js_reverse=FakeJsReverse(events, root),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> tuple[BrowserServiceError, dict[str, Any]]:
                await service.run(
                    OpenSessionRequest(
                        operation="open_session",
                        payload={"session_id": "close_unknown"},
                    )
                )
                playwright.fail_close = True
                with self.assertRaises(BrowserServiceError) as raised:
                    await service.run(
                        CloseSessionRequest(
                            operation="close_session",
                            payload={"session_id": "close_unknown"},
                        )
                    )
                saved = service.experiments.load_session("close_unknown")
                assert saved is not None
                return raised.exception, saved

            error, saved = asyncio.run(exercise())
            self.assertEqual(error.code, "operation_outcome_unknown")
            self.assertEqual(error.session_id, "close_unknown")
            self.assertEqual(saved["status"], "close_outcome_unknown")
            self.assertNotEqual(saved["status"], "open")

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

    def test_service_shutdown_detaches_all_states_that_may_hold_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            store = ExperimentStore(root)
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, root),
                experiments=store,
                default_browser_endpoint="http://127.0.0.1:9222",
            )
            statuses = [
                "open_unaligned",
                "open_outcome_unknown",
                "close_failed",
                "close_outcome_unknown",
            ]
            for index, status in enumerate(statuses):
                session_id = f"shutdown_uncertain_{index}"
                session = {
                    "session_id": session_id,
                    "status": status,
                    "service_instance_id": service.service_instance_id,
                    "updated_at": service.process_started_at,
                }
                service.sessions[session_id] = session
                store.save_session(session)

            asyncio.run(service.close())

            self.assertEqual(events.count("playwright.close"), len(statuses))
            for index in range(len(statuses)):
                saved = store.load_session(f"shutdown_uncertain_{index}")
                assert saved is not None
                self.assertEqual(saved["status"], "closed")
                self.assertEqual(saved["close_outcome"], "confirmed")

    def test_old_service_uncertain_sessions_are_marked_stale_on_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = ExperimentStore(root)
            for index, status in enumerate(["opening", "open_unaligned", "close_failed"]):
                store.save_session(
                    {
                        "session_id": f"old_{index}",
                        "status": status,
                        "service_instance_id": "svc_old",
                        "updated_at": "2026-01-01T00:00:00Z",
                    }
                )
            service = BrowserActionService(
                playwright=FakePlaywright([]),
                js_reverse=FakeJsReverse([], root),
                experiments=store,
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> list[dict[str, Any]]:
                return [
                    (
                        await service.inspect(
                            GetSessionRequest(
                                operation="get_session",
                                payload={"session_id": f"old_{index}"},
                            )
                        )
                    ).result["session"]
                    for index in range(3)
                ]

            inspected = asyncio.run(exercise())
            self.assertEqual([item["status"] for item in inspected], ["stale"] * 3)
            self.assertEqual(
                [item["previous_status"] for item in inspected],
                ["opening", "open_unaligned", "close_failed"],
            )

    def test_duplicate_nonterminal_session_id_is_rejected_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, root),
                experiments=ExperimentStore(root),
                default_browser_endpoint="http://127.0.0.1:9222",
            )

            async def exercise() -> BrowserServiceError:
                request = OpenSessionRequest(
                    operation="open_session",
                    payload={"session_id": "duplicate_session"},
                )
                await service.run(request)
                with self.assertRaises(BrowserServiceError) as raised:
                    await service.run(request)
                return raised.exception

            error = asyncio.run(exercise())
            self.assertEqual(error.code, "session_id_in_use")
            self.assertFalse(error.dispatch_started)
            self.assertEqual(error.session_id, "duplicate_session")
            self.assertEqual(events.count("playwright.open"), 1)

    def test_open_cancellation_persists_dispatch_boundary_and_reuse_fact(self) -> None:
        class CanceledOpenPlaywright(FakePlaywright):
            def __init__(
                self,
                events: list[str],
                *,
                sent: bool,
                attached: bool,
                stage: str,
            ) -> None:
                super().__init__(events)
                self.sent = sent
                self.attached = attached
                self.stage = stage

            async def open_session(
                self,
                session_ref: str,
                browser_endpoint: str,
                start_url: str | None,
                deadline: Deadline,
            ) -> PageState:
                error = asyncio.CancelledError()
                error.adapter_dispatch_started = self.sent
                error.session_attached = self.attached
                error.playwright_stage = self.stage
                raise error

        for sent, attached, stage, expected_status, expected_attach, expected_page in [
            (
                False,
                False,
                "attach",
                "open_canceled_before_dispatch",
                "canceled",
                "not_started",
            ),
            (
                True,
                True,
                "current_page",
                "open_unaligned",
                "confirmed",
                "unknown",
            ),
        ]:
            with (
                self.subTest(sent=sent, stage=stage),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                events: list[str] = []
                service = BrowserActionService(
                    playwright=CanceledOpenPlaywright(
                        events,
                        sent=sent,
                        attached=attached,
                        stage=stage,
                    ),
                    js_reverse=FakeJsReverse(events, root),
                    experiments=ExperimentStore(root),
                    default_browser_endpoint="http://127.0.0.1:9222",
                )

                async def exercise(
                    service: BrowserActionService = service,
                    sent: bool = sent,
                    events: list[str] = events,
                ) -> dict[str, Any]:
                    with self.assertRaises(asyncio.CancelledError):
                        await service.run(
                            OpenSessionRequest(
                                operation="open_session",
                                payload={"session_id": "canceled_open"},
                            )
                        )
                    saved = service.experiments.load_session("canceled_open")
                    assert saved is not None
                    if not sent:
                        service.playwright = FakePlaywright(events)
                        reopened = await service.run(
                            OpenSessionRequest(
                                operation="open_session",
                                payload={"session_id": "canceled_open"},
                            )
                        )
                        self.assertEqual(reopened.status, "completed")
                    return saved

                saved = asyncio.run(exercise())
                self.assertEqual(saved["status"], expected_status)
                self.assertEqual(saved["attach_outcome"], expected_attach)
                self.assertEqual(saved["page_selection_outcome"], expected_page)

    def test_close_cancellation_persists_before_and_after_dispatch_facts(self) -> None:
        class CanceledClosePlaywright(FakePlaywright):
            def __init__(self, events: list[str], *, sent: bool) -> None:
                super().__init__(events)
                self.sent = sent

            async def close_session(self, session_ref: str, deadline: Deadline) -> None:
                error = asyncio.CancelledError()
                error.adapter_dispatch_started = self.sent
                raise error

        for sent, expected_status, expected_outcome in [
            (False, "open", "canceled_before_dispatch"),
            (True, "close_outcome_unknown", "unknown"),
        ]:
            with self.subTest(sent=sent), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                events: list[str] = []
                playwright = CanceledClosePlaywright(events, sent=sent)
                service = BrowserActionService(
                    playwright=playwright,
                    js_reverse=FakeJsReverse(events, root),
                    experiments=ExperimentStore(root),
                    default_browser_endpoint="http://127.0.0.1:9222",
                )

                async def exercise(
                    service: BrowserActionService = service,
                ) -> dict[str, Any]:
                    await service.run(
                        OpenSessionRequest(
                            operation="open_session",
                            payload={"session_id": "canceled_close"},
                        )
                    )
                    with self.assertRaises(asyncio.CancelledError):
                        await service.run(
                            CloseSessionRequest(
                                operation="close_session",
                                payload={"session_id": "canceled_close"},
                            )
                        )
                    saved = service.experiments.load_session("canceled_close")
                    assert saved is not None
                    return saved

                saved = asyncio.run(exercise())
                self.assertEqual(saved["status"], expected_status)
                self.assertEqual(saved["close_outcome"], expected_outcome)

    def test_no_attachment_session_closes_locally_without_adapter_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            events: list[str] = []
            store = ExperimentStore(root)
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, root),
                experiments=store,
                default_browser_endpoint="http://127.0.0.1:9222",
            )
            session = {
                "session_id": "never_attached",
                "status": "open_failed_before_dispatch",
                "service_instance_id": service.service_instance_id,
                "updated_at": service.process_started_at,
            }
            service.sessions["never_attached"] = session
            store.save_session(session)

            response = asyncio.run(
                service.run(
                    CloseSessionRequest(
                        operation="close_session",
                        payload={"session_id": "never_attached"},
                    )
                )
            )
            saved = store.load_session("never_attached")
            assert saved is not None
            self.assertEqual(response.status, "completed")
            self.assertEqual(saved["status"], "closed")
            self.assertEqual(saved["close_outcome"], "not_required")
            self.assertNotIn("playwright.close", events)

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
