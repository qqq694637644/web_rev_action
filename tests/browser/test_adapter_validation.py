from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from skill_temple.browser.adapters.contracts import (
    AdapterError,
    CommandResult,
    PageState,
)
from skill_temple.browser.adapters.js_reverse import JsReverseMcpAdapter
from skill_temple.browser.adapters.playwright import PlaywrightCliAdapter
from skill_temple.browser_models import RequestMatcher, WaitCondition
from skill_temple.browser_service import Deadline
from tests.browser.common import BrowserActionTestCase


class AdapterValidationBrowserTests(BrowserActionTestCase):
    def test_page_wait_command_failures_are_not_interpreted_as_conditions(self) -> None:
        class FailingRunner:
            def __init__(self) -> None:
                self.allow_failure_values: list[bool] = []

            async def run(
                self,
                argv: list[str],
                *,
                deadline: Deadline,
                cwd: Path | None = None,
                allow_failure: bool = False,
            ) -> CommandResult:
                self.allow_failure_values.append(allow_failure)
                raise AdapterError(
                    "playwright command failed",
                    dispatch_started=True,
                    outcome_unknown=False,
                )

        for condition in [
            WaitCondition(
                type="selector_hidden",
                locator={"role": "button", "name": "Missing"},
                timeout_ms=100,
            ),
            WaitCondition(type="request_log_stable", timeout_ms=100),
        ]:
            with self.subTest(condition=condition.type):
                runner = FailingRunner()
                adapter = PlaywrightCliAdapter(runner=runner)
                with self.assertRaises(AdapterError):
                    asyncio.run(
                        adapter.wait_for_page_condition(
                            "wait-failure",
                            condition,
                            Deadline(1_000),
                        )
                    )
                self.assertEqual(runner.allow_failure_values, [False])

    def test_js_reverse_rejects_operation_level_shape_errors(self) -> None:
        class ShapeTransport:
            def __init__(self, result: dict[str, Any]) -> None:
                self.result = result

            @property
            def generation(self) -> int:
                return 1

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                return self.result

            async def close(self) -> None:
                return None

        async def invalid_alignment() -> None:
            adapter = JsReverseMcpAdapter(ShapeTransport({"unexpected": "shape"}))
            await adapter.align_page(
                PageState(url="https://example.test", page_index=0),
                Deadline(1_000),
            )

        async def invalid_start() -> None:
            adapter = JsReverseMcpAdapter(
                ShapeTransport({"capture": {"captureId": 0}})
            )
            await adapter.start_stream_capture(
                experiment_id="exp_invalid",
                matcher=RequestMatcher(),
                include_in_flight=False,
                deadline=Deadline(1_000),
            )

        async def invalid_network_page() -> None:
            adapter = JsReverseMcpAdapter(
                ShapeTransport({"requests": [], "pagination": {"hasNextPage": False}})
            )
            await adapter.list_network_requests(RequestMatcher(), Deadline(1_000))

        async def invalid_stream_page() -> None:
            adapter = JsReverseMcpAdapter(
                ShapeTransport(
                    {
                        "capture": {"captureId": 7},
                        "requests": [],
                        "pagination": {"hasNextPage": False},
                    }
                )
            )
            await adapter.get_stream_status(7, Deadline(1_000))

        async def invalid_console_page() -> None:
            adapter = JsReverseMcpAdapter(
                ShapeTransport(
                    {"messages": [], "pagination": {"hasNextPage": False}}
                )
            )
            await adapter.list_console_messages(Deadline(1_000))

        for operation, expected_path in [
            (invalid_alignment, "/pages"),
            (invalid_start, "/capture/captureId"),
            (invalid_network_page, "/pagination/totalPages"),
            (invalid_stream_page, "/pagination/totalPages"),
            (invalid_console_page, "/pagination/totalPages"),
        ]:
            with self.subTest(path=expected_path), self.assertRaises(AdapterError) as raised:
                asyncio.run(operation())
            self.assertEqual(raised.exception.code, "invalid_adapter_response")
            self.assertTrue(raised.exception.dispatch_started)
            self.assertIn(expected_path, str(raised.exception))
