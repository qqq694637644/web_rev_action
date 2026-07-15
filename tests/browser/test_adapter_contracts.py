from __future__ import annotations

import asyncio
import inspect
import tempfile
from pathlib import Path

from skill_temple.browser.adapters.contracts import (
    AlignmentResult,
    JsReverseAdapter,
    PageState,
    PlaywrightAdapter,
)
from skill_temple.browser_service import Deadline
from tests.fakes.browser import FakeJsReverse, FakePlaywright
from tests.fakes.scenarios import (
    BrowserScenario,
    artifact_failure_scenario,
    cancellation_scenario,
    network_request,
    stream_status,
    timeout_scenario,
)


def public_contract_methods(contract: type) -> set[str]:
    return {
        name
        for name, value in inspect.getmembers(contract)
        if not name.startswith("_") and (inspect.isfunction(value) or isinstance(value, property))
    }


def test_playwright_fake_implements_external_contract_surface() -> None:
    required = public_contract_methods(PlaywrightAdapter)
    missing = {name for name in required if not hasattr(FakePlaywright, name)}
    assert not missing


def test_js_reverse_fake_implements_external_contract_surface() -> None:
    required = public_contract_methods(JsReverseAdapter)
    missing = {name for name in required if not hasattr(FakeJsReverse, name)}
    assert not missing


def test_scenario_builder_returns_contract_shaped_adapters() -> None:
    async def exercise() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            playwright, js_reverse, events = BrowserScenario().build(Path(temp_dir))
            page = await playwright.open_session(
                "fixture-session",
                "http://127.0.0.1:9222",
                "https://fixture.test/app",
                Deadline(5_000),
            )
            alignment = await js_reverse.align_page(page, Deadline(5_000))

        assert isinstance(page, PageState)
        assert isinstance(alignment, AlignmentResult)
        assert alignment.status == "aligned"
        assert events == ["playwright.open", "js.align"]

    asyncio.run(exercise())


def test_scenario_builders_express_failures_without_business_semantics() -> None:
    assert artifact_failure_scenario().artifact_integrity == "failed"
    assert timeout_scenario().primary_status == "timed_out"
    assert cancellation_scenario().primary_status == "canceled"

    request = network_request(status=429)
    status = stream_status(raw_events=2, semantic_events=1)
    assert request["url"] == "https://fixture.test/api/resource"
    assert request["status"] == 429
    assert status["requests"][0]["rawEventCount"] == 2
    assert status["requests"][0]["semanticEventCount"] == 1
