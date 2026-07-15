from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.fakes.browser import FakeJsReverse, FakePlaywright


@dataclass(frozen=True, slots=True)
class BrowserScenario:
    """Composable external-adapter behavior without protocol conclusions."""

    alignment_status: str = "aligned"
    primary_status: str = "finished"
    raw_capture_integrity: str = "complete"
    semantic_parse_integrity: str = "complete"
    request_snapshot_integrity: str = "complete"
    artifact_integrity: str = "complete"
    include_supporting_failure: bool = False
    fail_stop: bool = False
    fail_step: str | None = None
    post_alignment_status: str | None = None

    def build(
        self,
        root: Path,
        events: list[str] | None = None,
    ) -> tuple[FakePlaywright, FakeJsReverse, list[str]]:
        event_log = events if events is not None else []
        playwright = FakePlaywright(event_log, fail_step=self.fail_step)
        js_reverse = FakeJsReverse(
            event_log,
            root,
            alignment_status=self.alignment_status,
            include_supporting_failure=self.include_supporting_failure,
            primary_status=self.primary_status,
            raw_capture_integrity=self.raw_capture_integrity,
            semantic_parse_integrity=self.semantic_parse_integrity,
            request_snapshot_integrity=self.request_snapshot_integrity,
            artifact_integrity=self.artifact_integrity,
            fail_stop=self.fail_stop,
            post_alignment_status=self.post_alignment_status,
        )
        return playwright, js_reverse, event_log


def network_request(
    *,
    request_id: str = "request-1",
    url: str = "https://fixture.test/api/resource",
    method: str = "POST",
    resource_type: str = "fetch",
    status: int | None = 200,
) -> dict[str, Any]:
    """Build one generic request fact for matcher and evidence tests."""
    return {
        "reqid": request_id,
        "cdpRequestId": request_id,
        "url": url,
        "method": method,
        "resourceType": resource_type,
        "status": status,
        "observedAtWallTimeMs": 1_700_000_000_000,
    }


def stream_status(
    *,
    request_id: str = "request-1",
    status: str = "finished",
    raw_events: int = 1,
    semantic_events: int = 1,
) -> dict[str, Any]:
    """Build a generic stream status payload matching the adapter contract."""
    return {
        "capture": {"captureId": 1, "version": 1, "status": "stopped"},
        "requests": [
            {
                "cdpRequestId": request_id,
                "persistentRequestId": f"persistent-{request_id}",
                "url": "https://fixture.test/api/resource",
                "method": "POST",
                "resourceType": "fetch",
                "status": status,
                "responseObserved": True,
                "rawEventCount": raw_events,
                "semanticEventCount": semantic_events,
                "primaryEventSource": "raw-stream",
            }
        ],
    }


def artifact_failure_scenario() -> BrowserScenario:
    return BrowserScenario(artifact_integrity="failed")


def timeout_scenario() -> BrowserScenario:
    return BrowserScenario(primary_status="timed_out")


def cancellation_scenario() -> BrowserScenario:
    return BrowserScenario(primary_status="canceled")
