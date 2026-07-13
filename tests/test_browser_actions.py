from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.browser_adapters import (
    AdapterError,
    AlignmentResult,
    JsReverseMcpAdapter,
    McpToolCallError,
    PageState,
    StdioMcpToolTransport,
    StreamCheckpoint,
    StreamRequestCheckpoint,
    StreamWaitResult,
    SubprocessCommandRunner,
    build_playwright_attach_args,
)
from skill_temple.browser_models import (
    CancelExperimentRequest,
    CaptureFlowRequest,
    ExactDataPredicate,
    FlowStep,
    Locator,
    OpenSessionRequest,
    RequestMatcher,
    WaitCondition,
)
from skill_temple.browser_service import (
    BrowserActionService,
    BrowserServiceError,
    Deadline,
    ExperimentStore,
    build_browser_service_from_environment,
)
from skill_temple.runtime_coordinator import RuntimeCoordinator, RuntimeOwner
from skill_temple.workspace_models import WorkspaceWriteFileRequest
from skill_temple.workspace_service import AnalysisWorkspaceService
from skill_temple.workspace_text_ops import WorkspaceToolError


class FakePlaywright:
    def __init__(self, events: list[str], *, fail_step: str | None = None) -> None:
        self.events = events
        self.fail_step = fail_step
        self.page = PageState(url="https://example.test/app", title="Example")

    async def open_session(
        self,
        session_ref: str,
        browser_endpoint: str,
        start_url: str | None,
        deadline: Deadline,
    ) -> PageState:
        self.events.append("playwright.open")
        if start_url:
            self.page = PageState(url=start_url, title="Example")
        return self.page

    async def current_page(self, session_ref: str, deadline: Deadline) -> PageState:
        self.events.append("playwright.current_page")
        return self.page

    async def select_page(
        self,
        session_ref: str,
        page_index: int,
        deadline: Deadline,
    ) -> PageState:
        self.events.append(f"playwright.select_page:{page_index}")
        self.page = PageState(
            url=self.page.url,
            title=self.page.title,
            page_index=page_index,
        )
        return self.page

    async def execute_step(
        self,
        session_ref: str,
        step: FlowStep,
        experiment_dir: Path,
        deadline: Deadline,
    ) -> dict[str, Any]:
        self.events.append(f"playwright.step:{step.step_id}")
        if step.step_id == self.fail_step:
            raise RuntimeError("synthetic step failure")
        if step.action == "navigate" and step.value:
            self.page = PageState(url=step.value, title="Example")
        snapshot = experiment_dir / "playwright" / f"{step.step_id}.yaml"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("snapshot\n", encoding="utf-8")
        return {"snapshot_ref": snapshot.as_posix()}

    async def wait_for_page_condition(
        self,
        session_ref: str,
        condition: WaitCondition,
        deadline: Deadline,
    ) -> dict[str, Any]:
        self.events.append(f"playwright.wait:{condition.type}")
        return {"condition_met": True, "type": condition.type}

    async def start_trace(self, session_ref: str, deadline: Deadline) -> None:
        self.events.append("playwright.trace_start")

    async def stop_trace(
        self,
        session_ref: str,
        experiment_dir: Path,
        deadline: Deadline,
        *,
        collect_files: bool = True,
    ) -> list[str]:
        self.events.append("playwright.trace_stop")
        if not collect_files:
            return []
        trace = experiment_dir / "playwright" / "trace.zip"
        trace.write_bytes(b"trace")
        return [trace.as_posix()]

    async def capture_screenshot(
        self,
        session_ref: str,
        experiment_dir: Path,
        name: str,
        deadline: Deadline,
    ) -> str:
        self.events.append(f"playwright.screenshot:{name}")
        screenshot = experiment_dir / "playwright" / "screenshots" / f"{name}.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        screenshot.write_bytes(b"png")
        return screenshot.as_posix()

    async def capture_snapshot(
        self,
        session_ref: str,
        experiment_dir: Path,
        name: str,
        deadline: Deadline,
    ) -> str:
        self.events.append(f"playwright.snapshot:{name}")
        snapshot = experiment_dir / "playwright" / "snapshots" / f"{name}.yaml"
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text("- button: Send\n", encoding="utf-8")
        return snapshot.as_posix()

    async def close_session(self, session_ref: str, deadline: Deadline) -> None:
        self.events.append("playwright.close")


class FakeJsReverse:
    def __init__(
        self,
        events: list[str],
        evidence_root: Path,
        *,
        alignment_status: str = "aligned",
        include_supporting_failure: bool = True,
        primary_status: str = "finished",
        raw_capture_integrity: str = "complete",
        semantic_parse_integrity: str = "complete",
        request_snapshot_integrity: str = "complete",
        artifact_integrity: str = "complete",
        fail_stop: bool = False,
        post_alignment_status: str | None = None,
    ) -> None:
        self.events = events
        self.evidence_root = evidence_root
        self.alignment_status = alignment_status
        self.include_supporting_failure = include_supporting_failure
        self.primary_status = primary_status
        self.raw_capture_integrity = raw_capture_integrity
        self.semantic_parse_integrity = semantic_parse_integrity
        self.request_snapshot_integrity = request_snapshot_integrity
        self.artifact_integrity = artifact_integrity
        self.fail_stop = fail_stop
        self.post_alignment_status = post_alignment_status
        self.start_arguments: dict[str, Any] | None = None
        self.capture_id = 1
        self.experiment_id = ""
        self.status_payload: dict[str, Any] = {}
        self.aligned_page_ids: list[str | None] = []
        self.alignment_calls = 0
        self.network_calls = 0
        self.console_calls = 0
        self.replay_count = 0
        self.replay_specs: dict[int, dict[str, Any]] = {}
        self.ignore_replay_spec_for_reqids: set[int] = set()
        self.network_response_content_type: str | None = "application/json"
        self.replay_response_status = 200
        self.replay_body_preview = '{"ok":true}'
        self.replay_redirected = False
        self.replay_final_url: str | None = None
        self.duplicate_next_replay_requests = 0
        self.replay_done_marker_observed: bool | None = None
        self.replay_termination_reason: str | None = None
        self.replay_done_event_name_observed: str | None = "message"
        self.replay_truncated = False
        self.wire_cookie_value: str | None = None
        self.omit_observed_at_reqids: set[int] = set()
        self.response_body_available = True
        self.response_headers_available = True
        self.extra_same_endpoint_stream: dict[str, Any] | None = None
        self.setup_output_response: dict[str, Any] | None = None
        self.setup_output_step_id = "setup_create"

    @property
    def transport_generation(self) -> int:
        return 1

    async def align_page(
        self,
        page: PageState,
        deadline: Deadline,
        page_id: str | None = None,
    ) -> AlignmentResult:
        self.events.append("js.align")
        self.aligned_page_ids.append(page_id)
        self.alignment_calls += 1
        effective_status = (
            self.post_alignment_status
            if self.post_alignment_status is not None and self.alignment_calls >= 3
            else self.alignment_status
        )
        if effective_status != "aligned":
            return AlignmentResult(
                status="not_aligned",
                playwright_page=page,
                warnings=["synthetic alignment failure"],
            )
        return AlignmentResult(
            status="aligned",
            playwright_page=page,
            js_reverse_page_index=0,
            js_reverse_page_id=page_id or "page_fake",
            js_reverse_page_url=page.url,
        )

    def _write_artifacts(self, experiment_id: str) -> dict[str, dict[str, Any]]:
        request_dir = (
            self.evidence_root
            / "experiments"
            / experiment_id
            / "js-reverse"
            / "capture-fake"
            / "request-0001"
        )
        request_dir.mkdir(parents=True, exist_ok=True)
        events_path = request_dir / "events.jsonl"
        events_path.write_text(
            json.dumps(
                {
                    "index": 0,
                    "eventName": "message",
                    "data": "[DONE]",
                    "defaultDoneMarker": True,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        full_headers = request_dir / "request-headers.json"
        full_headers.write_text(json.dumps({"authorization": "Bearer secret"}), encoding="utf-8")
        redacted_headers = request_dir / "request-headers.redacted.json"
        redacted_headers.write_text(json.dumps({"authorization": "[REDACTED]"}), encoding="utf-8")
        metadata_path = request_dir / "metadata.json"
        metadata_path.write_text(json.dumps({"status": "finished"}), encoding="utf-8")

        prefix = f"experiments/{experiment_id}/js-reverse/capture-fake/request-0001"
        return {
            "events": {
                "artifactId": f"art_{experiment_id}_events",
                "kind": "events",
                "relativePath": f"{prefix}/events.jsonl",
                "sensitivity": "private",
            },
            "headers": {
                "artifactId": f"art_{experiment_id}_headers",
                "kind": "request_headers",
                "relativePath": f"{prefix}/request-headers.json",
                "sensitivity": "credential",
                "containsCredentials": True,
                "redactedArtifactId": f"art_{experiment_id}_headers_redacted",
            },
            "redacted": {
                "artifactId": f"art_{experiment_id}_headers_redacted",
                "kind": "request_headers_redacted",
                "relativePath": f"{prefix}/request-headers.redacted.json",
                "sensitivity": "public",
                "containsCredentials": False,
            },
            "metadata": {
                "artifactId": f"art_{experiment_id}_request_metadata",
                "kind": "request_metadata",
                "relativePath": f"{prefix}/metadata.json",
                "sensitivity": "private",
            },
        }

    async def start_stream_capture(
        self,
        *,
        experiment_id: str,
        matcher: RequestMatcher,
        include_in_flight: bool,
        deadline: Deadline,
    ) -> dict[str, Any]:
        self.events.append("js.start")
        self.experiment_id = experiment_id
        self.start_arguments = {
            "experiment_id": experiment_id,
            "matcher": matcher.model_dump(exclude_none=True),
            "include_in_flight": include_in_flight,
        }
        artifacts = self._write_artifacts(experiment_id)
        primary = {
            "cdpRequestId": "primary-cdp",
            "persistentRequestId": f"req_{experiment_id}_primary",
            "networkRequestId": "network-primary",
            "collectorGeneration": 1,
            "url": "https://example.test/conversation",
            "method": "POST",
            "resourceType": "fetch",
            "status": self.primary_status,
            "terminalReason": (
                "network_canceled" if self.primary_status == "canceled" else "completed"
            ),
            "integrityStatus": "complete",
            "rawCaptureIntegrity": self.raw_capture_integrity,
            "semanticParseIntegrity": self.semantic_parse_integrity,
            "requestSnapshotIntegrity": self.request_snapshot_integrity,
            "artifactIntegrity": self.artifact_integrity,
            "responseObserved": True,
            "defaultDoneMarkerObserved": True,
            "rawEventCount": 1,
            "semanticEventCount": 0,
            "primaryEventSource": "raw-stream",
            "coreArtifacts": list(artifacts.values()),
        }
        requests = [primary]
        if self.extra_same_endpoint_stream is not None:
            requests.append(dict(self.extra_same_endpoint_stream))
        if self.include_supporting_failure:
            requests.append(
                {
                    "cdpRequestId": "telemetry-cdp",
                    "persistentRequestId": f"req_{experiment_id}_telemetry",
                    "url": "https://example.test/telemetry",
                    "method": "POST",
                    "resourceType": "fetch",
                    "status": "failed",
                    "integrityStatus": "failed",
                }
            )
        self.status_payload = {
            "capture": {
                "captureId": self.capture_id,
                "captureUuid": "11111111-1111-4111-8111-111111111111",
                "status": "capturing",
                "integrityStatus": "failed",
                "collectorIntegrity": "failed",
                "captureScope": "page-target-only",
                "workerCoverage": False,
                "version": 2,
            },
            "requests": requests,
        }
        return {"capture": self.status_payload["capture"]}

    async def get_stream_status(
        self,
        capture_id: int,
        deadline: Deadline,
        *,
        request_id: str | None = None,
        event_predicate: Any | None = None,
        after_event_index: int = -1,
        event_source: str | None = None,
    ) -> dict[str, Any]:
        self.events.append("js.status")
        if self.primary_status == "canceled":
            self.status_payload["requests"][0]["endedWallTimeMs"] = int(time.time() * 1000)
        return self.status_payload

    async def list_network_requests(
        self, matcher: RequestMatcher, deadline: Deadline
    ) -> dict[str, Any]:
        self.events.append("js.network")
        self.network_calls += 1
        requests = [
            {
                "reqid": 1,
                "url": "https://example.test/preexisting",
                "method": "GET",
                "resourceType": "fetch",
                "status": "[success - 200]",
                "pending": False,
            }
        ]
        if self.network_calls > 1:
            requests.append(
                {
                    "reqid": 2,
                    "networkRequestId": "network-2",
                    "cdpRequestId": "cdp-2",
                    "persistentRequestId": "persistent-2",
                    "collectorGeneration": 1,
                    "url": "https://example.test/conversation",
                    "method": "POST",
                    "resourceType": "fetch",
                    "status": "[success - 200]",
                    "pending": False,
                }
            )
            if not self.replay_specs and self.status_payload.get("requests"):
                primary = self.status_payload["requests"][0]
                primary["networkRequestId"] = "network-2"
                primary["collectorGeneration"] = 1
        if (
            self.setup_output_response is not None
            and f"playwright.step:{self.setup_output_step_id}" in self.events
        ):
            requests.append(
                {
                    "reqid": 50,
                    "networkRequestId": "network-50",
                    "cdpRequestId": "cdp-50",
                    "persistentRequestId": "persistent-50",
                    "collectorGeneration": 1,
                    "url": "https://example.test/api/conversations",
                    "method": "POST",
                    "resourceType": "fetch",
                    "status": "[success - 200]",
                    "pending": False,
                }
            )
        for reqid in sorted(self.replay_specs):
            requests.append(
                {
                    "reqid": reqid,
                    "networkRequestId": f"network-{reqid}",
                    "cdpRequestId": f"cdp-{reqid}",
                    "persistentRequestId": f"persistent-{reqid}",
                    "collectorGeneration": 1,
                    "url": "https://example.test/conversation",
                    "method": "POST",
                    "resourceType": "fetch",
                    "status": "[success - 200]",
                    "pending": False,
                }
            )
        return {"requests": requests}

    async def export_network_request(
        self,
        reqid: int,
        output_file: Path,
        output_part: str,
        deadline: Deadline,
    ) -> dict[str, Any]:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        replay_spec = (
            None if reqid in self.ignore_replay_spec_for_reqids else self.replay_specs.get(reqid)
        )
        request_body = (
            replay_spec.get("body")
            if isinstance(replay_spec, dict)
            else {
                "available": True,
                "size": 160,
                "encoding": "utf8",
                "text": json.dumps(
                    {
                        "messages": [
                            {
                                "id": "source-message-id",
                                "author": {"role": "user"},
                                "content": {"parts": ["hello"]},
                            }
                        ],
                        "parent_message_id": "source-parent-id",
                        "model": "fixture-model",
                        "tracking_id": "abc",
                    }
                ),
            }
        )
        request_headers = (
            replay_spec.get("headers")
            if isinstance(replay_spec, dict)
            else [
                {"name": "authorization", "value": "Bearer secret"},
                {"name": "content-type", "value": "application/json"},
                {"name": "cookie", "value": "session=secret"},
            ]
        )
        request_headers = list(request_headers)
        if replay_spec and self.wire_cookie_value is not None:
            request_headers.append({"name": "cookie", "value": self.wire_cookie_value})
        response_status = self.replay_response_status if replay_spec else 200
        response_text = (
            json.dumps(self.setup_output_response)
            if reqid == 50 and self.setup_output_response is not None
            else self.replay_body_preview
            if replay_spec
            else '{"ok":true}'
        )
        snapshot = {
            "url": (
                replay_spec.get("url")
                if isinstance(replay_spec, dict)
                else "https://example.test/api/conversations"
                if reqid == 50
                else "https://example.test/conversation?tracking=abc"
            ),
            "method": "POST",
            "resourceType": "fetch",
            "status": response_status,
            "statusText": "OK" if response_status < 400 else "Error",
            "networkRequestId": f"network-{reqid}",
            "cdpRequestId": f"cdp-{reqid}",
            "persistentRequestId": f"persistent-{reqid}",
            "collectorGeneration": 1,
            "requestHeadersArray": request_headers,
            "requestHeadersCompleteness": "complete",
            "responseHeadersArray": (
                [
                    {
                        "name": "content-type",
                        "value": self.network_response_content_type,
                    }
                ]
                if self.response_headers_available
                and self.network_response_content_type is not None
                else None
            ),
            "requestBody": request_body,
            "responseBody": {
                "available": self.response_body_available,
                "size": len(response_text.encode("utf-8")),
                "encoding": "utf8",
                **(
                    {"text": response_text}
                    if self.response_body_available
                    else {"reason": "response body unavailable"}
                ),
            },
        }
        if reqid not in self.omit_observed_at_reqids:
            snapshot["observedAt"] = int(time.time() * 1000)
        if output_part == "all":
            output_file.write_text(json.dumps(snapshot), encoding="utf-8")
        elif output_part in {"requestBody", "responseBody"}:
            key = output_part
            output_file.write_bytes(snapshot[key]["text"].encode("utf-8"))
        else:
            output_file.write_text(json.dumps(snapshot), encoding="utf-8")
        return {"filename": output_file.as_posix(), "byteLength": output_file.stat().st_size}

    async def get_request_initiator(self, reqid: int, deadline: Deadline) -> dict[str, Any]:
        return {
            "requestId": reqid,
            "initiator": {
                "type": "script",
                "stack": {"callFrames": [{"url": "https://example.test/app.js", "lineNumber": 12}]},
            },
        }

    async def search_scripts(
        self,
        query: str,
        deadline: Deadline,
        *,
        url_filter: str | None = None,
        max_results: int = 30,
        exclude_minified: bool = False,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "totalMatches": 1,
            "matches": [
                {
                    "scriptId": "script-1",
                    "url": "https://example.test/app.js",
                    "lineNumber": 12,
                    "lineContent": "buildConversationRequest(payload)",
                }
            ],
        }

    async def get_script_source(
        self,
        deadline: Deadline,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"source": "function buildConversationRequest(payload) { return payload; }"}

    async def list_console_messages(
        self,
        deadline: Deadline,
        *,
        types: list[str] | None = None,
        include_preserved_messages: bool = False,
    ) -> dict[str, Any]:
        self.console_calls += 1
        messages = [
            {
                "msgid": 1,
                "type": "warn",
                "text": "old warning",
                "url": "https://example.test/app.js",
            }
        ]
        if self.console_calls > 1:
            messages.append(
                {
                    "msgid": 2,
                    "type": "error",
                    "text": "new error",
                    "url": "https://example.test/app.js",
                    "lineNumber": 20,
                }
            )
        return {"messages": messages, "pagination": {"hasNextPage": False}}

    async def trace_cookie_provenance(self, cookie_name: str, deadline: Deadline) -> dict[str, Any]:
        return {
            "cookieName": cookie_name,
            "cookieFlow": [{"reqid": 2, "setCookieValues": [f"{cookie_name}=secret"]}],
        }

    async def evaluate_browser_replay(
        self,
        spec_file: Path,
        output_file: Path,
        deadline: Deadline,
    ) -> dict[str, Any]:
        self.events.append("js.replay")
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
        self.replay_count += 1
        reqid = max([2, *self.replay_specs.keys()]) + 1
        self.replay_specs[reqid] = spec
        for _ in range(self.duplicate_next_replay_requests):
            duplicate_reqid = max(self.replay_specs) + 1
            self.replay_specs[duplicate_reqid] = json.loads(json.dumps(spec))
        self.duplicate_next_replay_requests = 0
        if self.status_payload.get("requests"):
            primary = self.status_payload["requests"][0]
            primary["networkRequestId"] = f"network-{reqid}"
            primary["collectorGeneration"] = 1
        output_file.parent.mkdir(parents=True, exist_ok=True)
        is_stream = self.network_response_content_type == "text/event-stream"
        done_marker_observed = (
            self.replay_done_marker_observed
            if self.replay_done_marker_observed is not None
            else False
        )
        termination_reason = self.replay_termination_reason or ("network_close")
        response_mode = "sse" if is_stream else "ordinary"
        output_file.write_text(
            json.dumps(
                {
                    "status": self.replay_response_status,
                    "ok": 200 <= self.replay_response_status < 400,
                    "url": self.replay_final_url or spec["url"],
                    "redirected": self.replay_redirected,
                    "headers": (
                        [["content-type", self.network_response_content_type]]
                        if self.network_response_content_type is not None
                        else []
                    ),
                    "bodyByteLength": 11,
                    "bodyPreview": self.replay_body_preview,
                    "doneMarkerObserved": done_marker_observed,
                    "doneEventNameObserved": (
                        self.replay_done_event_name_observed if done_marker_observed else None
                    ),
                    "terminationReason": termination_reason,
                    "responseMode": response_mode,
                    "terminalConditionMatched": (
                        "exact_sse_data" if termination_reason == "done_marker" else "network_close"
                    ),
                    "truncated": self.replay_truncated,
                }
            ),
            encoding="utf-8",
        )
        return {
            "filename": output_file.as_posix(),
            "byteLength": output_file.stat().st_size,
            "resultType": "object",
        }

    async def wait_for_stream_condition(
        self,
        *,
        capture_id: int,
        request_matcher: RequestMatcher,
        condition: WaitCondition,
        checkpoint: StreamCheckpoint,
        deadline: Deadline,
    ) -> StreamWaitResult:
        self.events.append("js.wait")
        version = max(3, checkpoint.version + 1)
        matched_event = {
            "matched": True,
            "matchedEventIndex": version - 2,
            "matchedRequestId": "primary-cdp",
            "matchedSource": "raw-stream",
            "rawByteStart": (version - 2) * 16,
            "rawByteEnd": (version - 1) * 16,
        }
        return StreamWaitResult(
            condition_met=True,
            capture_id=capture_id,
            capture_version=version,
            matched_request_ids=["primary-cdp"],
            terminal_status=self.primary_status,
            matched_event=matched_event,
            checkpoint=StreamCheckpoint(
                version=version,
                requests={
                    "primary-cdp": StreamRequestCheckpoint(
                        response_observed=True,
                        status=self.primary_status,
                        terminal_wall_time_ms=(
                            float(
                                self.status_payload["requests"][0].get(
                                    "endedWallTimeMs",
                                    time.time() * 1000,
                                )
                            )
                            if self.primary_status in {"finished", "canceled", "failed", "stopped"}
                            else None
                        ),
                        raw_event_index=version - 2,
                        semantic_event_index=-1,
                        primary_event_source="raw-stream",
                    )
                },
            ),
            status_payload=self.status_payload,
        )

    async def stop_stream_capture(self, capture_id: int, deadline: Deadline) -> dict[str, Any]:
        self.events.append("js.stop")
        if self.fail_stop:
            raise RuntimeError("synthetic stop failure")
        self.status_payload["capture"]["status"] = "stopped"
        artifacts = self.status_payload["requests"][0]["coreArtifacts"]
        metadata = next(item for item in artifacts if item["kind"] == "request_metadata")
        return {
            "capture": self.status_payload["capture"],
            "captureMetadataArtifact": {
                "artifactId": f"art_{self.experiment_id}_capture",
                "kind": "capture_metadata",
                "relativePath": f"experiments/{self.experiment_id}/manifest.json",
            },
            "requestMetadataArtifacts": [metadata],
        }

    async def close(self) -> None:
        self.events.append("js.close")


class BrowserActionTests(unittest.TestCase):
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
    def capture_request(*, include_in_flight: bool = False) -> dict[str, Any]:
        return {
            "operation": "capture_flow",
            "payload": {
                "session_id": "session_one",
                "objective": "capture one conversation stream",
                "target": {"expected_url_contains": "/app"},
                "primary_request": {
                    "url_contains": "/conversation",
                    "method": "POST",
                    "resource_types": ["fetch"],
                    "expected_min_matches": 1,
                    "expected_max_matches": 1,
                    "allow_supporting_failures": True,
                    "include_in_flight": include_in_flight,
                },
                "flow": [
                    {
                        "step_id": "send_message",
                        "action": "fill",
                        "locator": {"placeholder": "Message"},
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
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                },
                "deadline_ms": 10_000,
                "execution_mode": "sync",
            },
        }

    def capture_source_and_control(
        self,
        client: TestClient,
        root: Path,
        *,
        volatile_bindings: list[dict[str, Any]] | None = None,
    ) -> tuple[str, dict[str, Any], str, dict[str, Any]]:
        capture = self.capture_request()
        capture["payload"]["network_evidence"] = [
            {
                "selector_id": "conversation_submit",
                "matcher": {
                    "url_contains": "/conversation",
                    "method": "POST",
                },
                "export_parts": ["all"],
            }
        ]
        source = client.post("/v1/browser/run", json=capture)
        self.assertEqual(source.status_code, 200, source.text)
        source_id = source.json()["experiment_id"]
        source_manifest = json.loads(
            (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
        )
        source_evidence = next(
            item for item in source_manifest["evidence"] if item["kind"] == "network_request"
        )
        control = client.post(
            "/v1/browser/run",
            json={
                "operation": "replay_request",
                "payload": {
                    "session_id": "session_one",
                    "objective": "establish a causal control replay",
                    "source_experiment_id": source_id,
                    "source_evidence_id": source_evidence["evidence_id"],
                    "replay_mode": "control",
                    "mutations": [],
                    "volatile_bindings": volatile_bindings
                    or [
                        {
                            "binding_id": "message_id",
                            "target": "json_pointer",
                            "path": "/messages/0/id",
                            "generator": "uuid4",
                            "reuse_policy": "fresh_equivalent",
                        }
                    ],
                    "execution_mode": "sync",
                    "deadline_ms": 10_000,
                    "capture": {
                        "network": True,
                        "stream": False,
                        "trace": False,
                        "screenshots": False,
                        "page_snapshots": False,
                        "console_errors": False,
                    },
                },
            },
        )
        self.assertEqual(control.status_code, 200, control.text)
        self.assertEqual(control.json()["status"], "completed", control.text)
        control_id = control.json()["experiment_id"]
        control_manifest = json.loads(
            (root / "experiments" / control_id / "manifest.json").read_text(encoding="utf-8")
        )
        return source_id, source_evidence, control_id, control_manifest

    def test_openapi_has_two_browser_actions_and_discriminated_unions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            schema = client.get("/openapi.json").json()
        run = schema["paths"]["/v1/browser/run"]["post"]
        inspect = schema["paths"]["/v1/browser/inspect"]["post"]
        self.assertIs(run["x-openai-isConsequential"], True)
        self.assertIs(inspect["x-openai-isConsequential"], False)
        run_schema = run["requestBody"]["content"]["application/json"]["schema"]
        inspect_schema = inspect["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("oneOf", run_schema)
        self.assertIn("discriminator", run_schema)
        self.assertIn("oneOf", inspect_schema)
        self.assertIn("discriminator", inspect_schema)
        run_variants = str(run_schema)
        inspect_variants = str(inspect_schema)
        self.assertIn("CancelExperimentRequest", run_variants)
        self.assertIn("ReplayRequestRequest", run_variants)
        self.assertIn("SaveScriptSourceRequest", run_variants)
        self.assertIn("GetStreamStatusRequest", inspect_variants)
        for variant in [
            "ListEvidenceRequest",
            "GetNetworkEvidenceRequest",
            "GetRequestShapeRequest",
            "GetRequestInitiatorRequest",
            "SearchScriptsRequest",
            "GetScriptSourceRequest",
            "ListConsoleErrorsRequest",
        ]:
            self.assertIn(variant, inspect_variants)
        status_payload = schema["components"]["schemas"]["GetStreamStatusPayload"]
        self.assertIn("experiment_id", status_payload["properties"])
        self.assertIn("capture_uuid", status_payload["properties"])
        self.assertNotIn("capture_id", status_payload["properties"])
        control_payload = schema["components"]["schemas"]["ReplayControlPayload"]
        treatment_payload = schema["components"]["schemas"]["ReplayTreatmentPayload"]
        self.assertIn("replay_mode", control_payload["required"])
        self.assertEqual(control_payload["properties"]["mutations"]["maxItems"], 0)
        self.assertIn("setup_flow", control_payload["properties"])
        self.assertIn("verification_flow", control_payload["properties"])
        for field in [
            "max_response_bytes",
            "stream_idle_timeout_ms",
            "default_done_marker",
            "default_done_event_name",
            "raw_only",
            "ignored_cookie_names",
            "ignored_context_headers",
        ]:
            self.assertIn(field, control_payload["properties"])
        binding_payload = schema["components"]["schemas"]["VolatileBinding"]
        self.assertIn("value_source", binding_payload["properties"])
        self.assertIn("reuse_policy", binding_payload["properties"])
        self.assertEqual(
            set(treatment_payload["properties"]),
            {"replay_mode", "control_experiment_id", "mutation"},
        )
        shape_payload = schema["components"]["schemas"]["GetRequestShapePayload"]
        for field in [
            "path_prefix",
            "page_idx",
            "page_size",
            "max_depth",
            "max_array_items",
            "include_redacted_body",
        ]:
            self.assertIn(field, shape_payload["properties"])

    def test_atomic_capture_order_manifest_and_primary_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, js = self.make_client(root)
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["status"], "completed")
            summary = body["result"]["experiment"]
            manifest = root / body["result"]["manifest_relative_path"]
            experiment = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(summary["execution_integrity"], "complete")
            self.assertEqual(summary["evidence_integrity"], "complete")
            self.assertEqual(experiment["primary_request_integrity"], "complete")
            self.assertNotIn("objective_integrity", experiment)
            self.assertEqual(experiment["collector_integrity"], "failed")
            self.assertTrue(experiment["capture_health"]["collector_stopped"])
            self.assertFalse(experiment["capture_health"]["worker_coverage"])
            artifact_kinds = {item["kind"] for item in experiment["artifacts"]}
            self.assertIn("playwright_screenshot", artifact_kinds)
            self.assertIn("playwright_trace", artifact_kinds)
            self.assertTrue(experiment["network_summary"]["requests"])
            self.assertLess(events.index("js.start"), events.index("playwright.step:send_message"))
            self.assertLess(events.index("playwright.step:click_send"), events.index("js.wait"))
            self.assertLess(events.index("js.wait"), events.index("js.stop"))
            self.assertTrue(manifest.is_file())
            self.assertEqual(js.start_arguments["include_in_flight"], False)

    def test_failure_still_stops_collector_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, _ = self.make_client(root, fail_step="click_send")
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["status"], "failed")
            self.assertIn("js.stop", events)
            manifest = root / body["result"]["manifest_relative_path"]
            saved = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "failed")
            self.assertTrue(saved["errors"])

    def test_alignment_failure_prevents_stream_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, events, _ = self.make_client(Path(temp_dir), alignment_status="not_aligned")
            with client:
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "open_session",
                        "payload": {"session_id": "session_one"},
                    },
                )
            self.assertEqual(response.status_code, 409)
            self.assertNotIn("js.start", events)
            self.assertIn("playwright.close", events)

    def test_include_in_flight_is_forwarded_and_close_session_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, js = self.make_client(root)
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run",
                    json=self.capture_request(include_in_flight=True),
                )
                close = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "close_session",
                        "payload": {"session_id": "session_one"},
                    },
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(js.start_arguments["include_in_flight"])
            self.assertEqual(close.status_code, 200)
            self.assertIn("playwright.close", events)
            session = json.loads(
                (root / "sessions" / "session_one.json").read_text(encoding="utf-8")
            )
            self.assertEqual(session["status"], "closed")

    def test_baseline_payload_uses_defaults_without_requiring_a_primary_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "capture_baseline",
                        "payload": {
                            "session_id": "session_one",
                            "execution_mode": "sync",
                        },
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            experiment = json.loads(
                (root / body["result"]["manifest_relative_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(experiment["primary_request_matcher"]["expected_min_matches"], 0)
            self.assertEqual(experiment["steps"], [])
            self.assertEqual(response.json()["status"], "completed")
            self.assertEqual(experiment["execution_integrity"], "complete")
            self.assertEqual(experiment["evidence_integrity"], "complete")

    def test_supporting_failure_can_be_made_objective_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            request["payload"]["primary_request"]["allow_supporting_failures"] = False
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            experiment = json.loads(
                (Path(temp_dir) / body["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(experiment["primary_request_integrity"], "complete")
            self.assertEqual(experiment["collector_integrity"], "failed")
            self.assertEqual(experiment["execution_integrity"], "complete")
            self.assertEqual(experiment["evidence_integrity"], "failed")

    def test_stop_intent_correlates_only_the_primary_network_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(
                Path(temp_dir),
                include_supporting_failure=False,
                primary_status="canceled",
            )
            request = self.capture_request()
            request["payload"]["flow"] = [
                {
                    "step_id": "wait_stream_started",
                    "action": "wait",
                    "condition": {
                        "type": "first_event",
                        "request_matcher": {
                            "url_contains": "/conversation",
                            "method": "POST",
                        },
                    },
                },
                {
                    "step_id": "stop_generation",
                    "action": "click",
                    "locator": {"role": "button", "name": "Stop"},
                    "intent": "stop_generation",
                },
            ]
            request["payload"]["wait_for"] = {
                "type": "network_canceled",
                "request_matcher": {
                    "url_contains": "/conversation",
                    "method": "POST",
                },
            }
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            experiment = json.loads(
                (Path(temp_dir) / body["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            classification = experiment["cancellation_classifications"][0]
            self.assertEqual(classification["classification"], "expected_user_cancel")
            self.assertTrue(classification["within_stop_window"])
            self.assertEqual(
                experiment["primary_requests"][0]["experimentCancellationClassification"],
                "expected_user_cancel",
            )
            self.assertIsNotNone(classification["stream_before_stop"])

    def test_stop_intent_without_stream_start_is_recorded_without_preclassification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                include_supporting_failure=False,
                primary_status="canceled",
            )
            request = self.capture_request()
            request["payload"]["flow"] = [
                {
                    "step_id": "stop_generation",
                    "action": "click",
                    "locator": {"role": "button", "name": "Stop"},
                    "intent": "stop_generation",
                }
            ]
            request["payload"]["wait_for"] = {
                "type": "network_canceled",
                "request_matcher": {"url_contains": "/conversation"},
            }
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 200, response.text)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                manifest["cancellation_classifications"][0]["classification"],
                "unclassified_network_cancel",
            )
            self.assertFalse(
                manifest["cancellation_classifications"][0]["same_request_observed"]
            )

    def test_job_mode_returns_running_then_completes_via_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            request["payload"].pop("execution_mode")
            request["payload"]["job_timeout_ms"] = 30_000
            with client:
                self.open_session(client)
                started = client.post("/v1/browser/run", json=request)
                self.assertEqual(started.status_code, 200, started.text)
                self.assertEqual(started.json()["status"], "running")
                experiment_id = started.json()["experiment_id"]
                final: dict[str, Any] | None = None
                for _ in range(100):
                    inspected = client.post(
                        "/v1/browser/inspect",
                        json={
                            "operation": "get_experiment",
                            "payload": {"experiment_id": experiment_id},
                        },
                    )
                    self.assertEqual(inspected.status_code, 200, inspected.text)
                    if inspected.json()["status"] != "running":
                        final = inspected.json()
                        break
                    time.sleep(0.02)
            self.assertIsNotNone(final)
            self.assertEqual(final["status"], "completed")
            manifest = Path(temp_dir) / final["result"]["manifest_relative_path"]
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8"))["execution_mode"],
                "job",
            )

    def test_experiment_store_recovers_running_manifest_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiment_dir = root / "experiments" / "exp_crashed"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp_crashed",
                        "session_id": "session_one",
                        "status": "running",
                        "errors": [],
                    }
                ),
                encoding="utf-8",
            )
            experiments = ExperimentStore(root)
            recovered = experiments.load_manifest("exp_crashed")
            self.assertEqual(recovered["status"], "interrupted")
            self.assertTrue(recovered["errors"])

    def test_session_reuses_stable_js_reverse_page_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, js = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(js.aligned_page_ids, [None, "page_fake", "page_fake"])

    def test_capture_without_page_index_reuses_session_selected_tab(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, events, _ = self.make_client(Path(temp_dir))
            with client:
                opened = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "open_session",
                        "payload": {
                            "session_id": "session_one",
                            "target": {"page_index": 2},
                        },
                    },
                )
                self.assertEqual(opened.status_code, 200, opened.text)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(events.count("playwright.select_page:2"), 2)
            self.assertNotIn("playwright.select_page:0", events)

    def test_capture_rejects_implicit_target_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = self.capture_request()
            request["payload"]["target"]["start_url"] = "https://example.test/new"
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 422)
            self.assertIn("explicit navigate flow step", response.text)

    def test_persisted_open_session_becomes_stale_after_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiments = ExperimentStore(root)
            experiments.save_session(
                {
                    "session_id": "session_old",
                    "status": "open",
                    "service_instance_id": "svc_old",
                    "playwright_session_ref": "session_old",
                    "playwright_page_index": 0,
                }
            )
            events: list[str] = []
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, root),
                experiments=experiments,
                default_browser_endpoint="http://127.0.0.1:9222",
            )
            client = TestClient(create_app(browser_service=service))
            with client:
                response = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_session",
                        "payload": {"session_id": "session_old"},
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["result"]["session"]["status"], "stale")

    def test_partial_raw_capture_produces_partial_objective(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                include_supporting_failure=False,
                raw_capture_integrity="partial",
            )
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "partial")
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["execution_integrity"], "complete")
            self.assertEqual(manifest["evidence_integrity"], "partial")
            self.assertEqual(
                manifest["primary_integrity_dimensions"]["raw_capture"],
                "partial",
            )

    def test_stop_failure_records_orphan_capture_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, _ = self.make_client(root, fail_stop=True)
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=self.capture_request())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "failed")
            self.assertIn("js.stop", events)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["capture_health"]["orphan_capture_id"], 1)
            self.assertEqual(
                manifest["capture_health"]["collector_cleanup"],
                "unknown",
            )

    def test_post_stop_alignment_failure_prevents_user_cancel_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                include_supporting_failure=False,
                primary_status="canceled",
                post_alignment_status="not_aligned",
            )
            request = self.capture_request()
            request["payload"]["flow"] = [
                {
                    "step_id": "wait_stream_started",
                    "action": "wait",
                    "condition": {
                        "type": "first_event",
                        "request_matcher": {"url_contains": "/conversation"},
                    },
                },
                {
                    "step_id": "stop_generation",
                    "action": "click",
                    "locator": {"role": "button", "name": "Stop"},
                    "intent": "stop_generation",
                },
            ]
            request["payload"]["wait_for"] = {
                "type": "network_canceled",
                "request_matcher": {"url_contains": "/conversation"},
            }
            with client:
                self.open_session(client)
                response = client.post("/v1/browser/run", json=request)
            manifest = json.loads(
                (root / response.json()["result"]["manifest_relative_path"]).read_text(
                    encoding="utf-8"
                )
            )
            classification = manifest["cancellation_classifications"][0]
            self.assertEqual(
                classification["classification"],
                "unclassified_network_cancel",
            )
            self.assertFalse(classification["page_remained_aligned"])

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
                                "url_contains": "/conversation",
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
                                    "url_contains": "/conversation",
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
                await asyncio.Event().wait()
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
                            "url_contains": "/conversation",
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

    def test_environment_builder_binds_mcp_to_workspace_and_same_cdp_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "WEB_REV_EVIDENCE_DIR": temp_dir,
                    "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
                    "WEB_REV_JS_REVERSE_COMMAND": "js-reverse-mcp",
                },
                clear=True,
            ):
                service = build_browser_service_from_environment()
            self.assertIsInstance(service.js_reverse, JsReverseMcpAdapter)
            transport = service.js_reverse.transport
            self.assertIsInstance(transport, StdioMcpToolTransport)
            self.assertEqual(
                transport.args,
                [
                    "--browserUrl",
                    "http://127.0.0.1:9222",
                    "--allowedRoots",
                    str(Path(temp_dir).resolve()),
                    "--streamArtifactRoot",
                    "0",
                ],
            )
            self.assertEqual(
                service.private_mcp_browser_endpoint,
                "http://127.0.0.1:9222",
            )

    def test_environment_builder_only_appends_non_conflicting_mcp_args(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                os.environ,
                {
                    "WEB_REV_EVIDENCE_DIR": temp_dir,
                    "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
                    "WEB_REV_JS_REVERSE_EXTRA_ARGS": '["--headless", "false"]',
                },
                clear=True,
            ):
                service = build_browser_service_from_environment()
            transport = service.js_reverse.transport
            self.assertEqual(
                transport.args,
                [
                    "--browserUrl",
                    "http://127.0.0.1:9222",
                    "--allowedRoots",
                    str(Path(temp_dir).resolve()),
                    "--streamArtifactRoot",
                    "0",
                    "--headless",
                    "false",
                ],
            )
            with patch.dict(
                os.environ,
                {
                    "WEB_REV_EVIDENCE_DIR": temp_dir,
                    "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
                    "WEB_REV_JS_REVERSE_EXTRA_ARGS": '["--browserUrl=http://bad"]',
                },
                clear=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "managed by web_rev_action"):
                    build_browser_service_from_environment()

    def test_configured_private_mcp_rejects_a_different_playwright_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            events: list[str] = []
            experiments = ExperimentStore(Path(temp_dir))
            service = BrowserActionService(
                playwright=FakePlaywright(events),
                js_reverse=FakeJsReverse(events, experiments.root),
                experiments=experiments,
                default_browser_endpoint="http://127.0.0.1:9222",
                private_mcp_browser_endpoint="http://127.0.0.1:9222",
                require_private_mcp_endpoint=True,
            )
            client = TestClient(create_app(browser_service=service))
            with client:
                response = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "open_session",
                        "payload": {
                            "session_id": "session_one",
                            "browser_endpoint": "http://127.0.0.1:9333",
                        },
                    },
                )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["detail"]["error"]["code"],
                "browser_endpoint_mismatch",
            )

    def test_private_js_reverse_adapter_calls_stream_primitives_with_namespace(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []

            async def call_tool(
                self, name: str, arguments: dict[str, Any], deadline: Deadline
            ) -> dict[str, Any]:
                self.calls.append((name, arguments))
                if name == "start_stream_capture":
                    return {"capture": {"captureId": 7, "version": 1}}
                if name == "get_stream_status":
                    payload = {
                        "capture": {"captureId": 7, "version": 2},
                        "requests": [
                            {
                                "cdpRequestId": "req-7",
                                "url": "https://example.test/conversation",
                                "method": "POST",
                                "resourceType": "fetch",
                                "status": "finished",
                                "rawEventCount": 52,
                                "semanticEventCount": 0,
                            }
                        ],
                    }
                    if "eventPredicate" in arguments:
                        payload["eventMatch"] = {
                            "matched": True,
                            "matchedEventIndex": 51,
                            "matchedRequestId": "req-7",
                            "matchedSource": "raw-stream",
                        }
                    return payload
                if name == "stop_stream_capture":
                    return {"capture": {"captureId": 7, "status": "stopped"}}
                return {}

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory():
            transport = FakeTransport()
            adapter = JsReverseMcpAdapter(
                transport,
            )

            async def exercise() -> StreamWaitResult:
                deadline = Deadline(5_000)
                started = await adapter.start_stream_capture(
                    experiment_id="exp_private",
                    matcher=RequestMatcher(
                        url_contains="/conversation",
                        method="POST",
                        resource_types=["fetch"],
                        mime_types=["text/event-stream"],
                    ),
                    include_in_flight=False,
                    deadline=deadline,
                )
                self.assertEqual(started["capture"]["captureId"], 7)
                waited = await adapter.wait_for_stream_condition(
                    capture_id=7,
                    request_matcher=RequestMatcher(url_contains="/conversation", method="POST"),
                    condition=WaitCondition(
                        type="event_predicate",
                        request_matcher=RequestMatcher(url_contains="/conversation", method="POST"),
                        predicate=ExactDataPredicate(
                            type="exact_data",
                            value="[DONE]",
                        ),
                    ),
                    checkpoint=StreamCheckpoint(
                        version=1,
                        requests={
                            "req-7": StreamRequestCheckpoint(
                                status="streaming",
                                raw_event_index=-1,
                                semantic_event_index=-1,
                                primary_event_source="raw-stream",
                            )
                        },
                    ),
                    deadline=deadline,
                )
                await adapter.stop_stream_capture(7, deadline)
                return waited

            waited = asyncio.run(exercise())
            self.assertTrue(waited.condition_met)
            self.assertEqual(
                [name for name, _ in transport.calls],
                [
                    "start_stream_capture",
                    "get_stream_status",
                    "get_stream_status",
                    "stop_stream_capture",
                ],
            )
            start_args = transport.calls[0][1]
            self.assertEqual(start_args["artifactNamespace"], "exp_private")
            self.assertFalse(start_args["includeInFlight"])
            self.assertEqual(start_args["mimeTypes"], ["text/event-stream"])
            status_args = transport.calls[2][1]
            self.assertEqual(
                status_args["eventPredicate"],
                {"type": "exact_data", "value": "[DONE]"},
            )
            self.assertEqual(status_args["afterEventIndex"], -1)
            self.assertEqual(waited.matched_event["matchedEventIndex"], 51)

    def test_stdio_mcp_transport_calls_a_real_mcp_server(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = root / "fake_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "from mcp.server.fastmcp import FastMCP",
                        "from pydantic import BaseModel",
                        "server = FastMCP('browser-test')",
                        "class Capture(BaseModel):",
                        "    captureId: int",
                        "    namespace: str",
                        "    includeInFlight: bool",
                        "class StartResult(BaseModel):",
                        "    capture: Capture",
                        "@server.tool(structured_output=True)",
                        "def start_stream_capture(",
                        "    artifactNamespace: str, includeInFlight: bool = False",
                        ") -> StartResult:",
                        "    return StartResult(capture=Capture(",
                        "        captureId=19,",
                        "        namespace=artifactNamespace,",
                        "        includeInFlight=includeInFlight,",
                        "    ))",
                        "if __name__ == '__main__':",
                        "    server.run(transport='stdio')",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            transport = StdioMcpToolTransport(
                command=sys.executable,
                args=[str(server)],
                cwd=root,
            )

            async def exercise() -> dict[str, Any]:
                try:
                    return await transport.call_tool(
                        "start_stream_capture",
                        {
                            "artifactNamespace": "exp_stdio",
                            "includeInFlight": False,
                        },
                        Deadline(10_000),
                    )
                finally:
                    await asyncio.create_task(transport.close())

            result = asyncio.run(exercise())
            self.assertEqual(result["capture"]["captureId"], 19)
            self.assertEqual(result["capture"]["namespace"], "exp_stdio")

    def test_expired_queued_side_effect_is_discarded_and_worker_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = root / "slow_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "import asyncio",
                        "from pathlib import Path",
                        "from mcp.server.fastmcp import FastMCP",
                        "from pydantic import BaseModel",
                        "server = FastMCP('slow-browser-test')",
                        "class Capture(BaseModel):",
                        "    captureId: int",
                        "    namespace: str",
                        "class StartResult(BaseModel):",
                        "    capture: Capture",
                        "@server.tool(structured_output=True)",
                        "async def start_stream_capture(",
                        "    artifactNamespace: str,",
                        "    marker: str,",
                        "    delayMs: int = 0,",
                        ") -> StartResult:",
                        "    await asyncio.sleep(delayMs / 1000)",
                        "    Path(marker).write_text(artifactNamespace, encoding='utf-8')",
                        "    return StartResult(capture=Capture(",
                        "        captureId=21, namespace=artifactNamespace",
                        "    ))",
                        "if __name__ == '__main__':",
                        "    server.run(transport='stdio')",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            transport = StdioMcpToolTransport(
                command=sys.executable,
                args=[str(server)],
                cwd=root,
            )
            first_marker = root / "first.txt"
            queued_marker = root / "queued.txt"
            restart_marker = root / "restart.txt"

            async def exercise() -> dict[str, Any]:
                first = asyncio.create_task(
                    transport.call_tool(
                        "start_stream_capture",
                        {
                            "artifactNamespace": "first",
                            "marker": str(first_marker),
                            "delayMs": 2_000,
                        },
                        Deadline(5_000),
                    )
                )
                await asyncio.sleep(0.1)
                with self.assertRaises(AdapterError):
                    await transport.call_tool(
                        "start_stream_capture",
                        {
                            "artifactNamespace": "queued",
                            "marker": str(queued_marker),
                            "delayMs": 0,
                        },
                        Deadline(150),
                    )
                await asyncio.gather(first, return_exceptions=True)
                await asyncio.sleep(0.3)
                result = await transport.call_tool(
                    "start_stream_capture",
                    {
                        "artifactNamespace": "restart",
                        "marker": str(restart_marker),
                        "delayMs": 0,
                    },
                    Deadline(5_000),
                )
                await transport.close()
                return result

            result = asyncio.run(exercise())
            self.assertFalse(queued_marker.exists())
            self.assertTrue(restart_marker.is_file())
            self.assertEqual(result["capture"]["namespace"], "restart")

    def test_strict_flow_schema_rejects_missing_locator_and_extra_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                request = self.capture_request()
                request["payload"]["flow"] = [
                    {"step_id": "bad", "action": "click", "unknown": True}
                ]
                response = client.post("/v1/browser/run", json=request)
            self.assertEqual(response.status_code, 422)

    def test_strict_flow_and_predicate_unions_reject_cross_action_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            with client:
                self.open_session(client)
                click_request = self.capture_request()
                click_request["payload"]["flow"] = [
                    {
                        "step_id": "bad_click",
                        "action": "click",
                        "locator": {"role": "button", "name": "Send"},
                        "value": "not-allowed",
                    }
                ]
                click_response = client.post(
                    "/v1/browser/run",
                    json=click_request,
                )
                fill_request = self.capture_request()
                fill_request["payload"]["flow"] = [
                    {
                        "step_id": "bad_fill",
                        "action": "fill",
                        "locator": {"placeholder": "Message"},
                        "value": "hello",
                        "intent": "stop_generation",
                    }
                ]
                fill_response = client.post(
                    "/v1/browser/run",
                    json=fill_request,
                )
                predicate_request = self.capture_request()
                predicate_request["payload"]["wait_for"] = {
                    "type": "event_predicate",
                    "request_matcher": {"url_contains": "/conversation"},
                    "predicate": {
                        "type": "exact_data",
                        "value": "[DONE]",
                        "path": "$.type",
                    },
                }
                predicate_response = client.post(
                    "/v1/browser/run",
                    json=predicate_request,
                )
            self.assertEqual(click_response.status_code, 422)
            self.assertEqual(fill_response.status_code, 422)
            self.assertEqual(predicate_response.status_code, 422)

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
                            "url": "https://example.test/conversation",
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
                request_matcher=RequestMatcher(url_contains="/conversation"),
                condition=WaitCondition(
                    type="first_event",
                    request_matcher=RequestMatcher(url_contains="/conversation"),
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
                            "url": "https://example.test/conversation",
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
                request_matcher=RequestMatcher(url_contains="/conversation"),
                condition=WaitCondition(
                    type="event_predicate",
                    request_matcher=RequestMatcher(url_contains="/conversation"),
                    predicate=ExactDataPredicate(
                        type="exact_data",
                        value="[DONE]",
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
                            "url": "https://example.test/conversation",
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
                            "url": "https://example.test/conversation",
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
                            "url": "https://example.test/conversation",
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
                request_matcher=RequestMatcher(url_contains="/conversation"),
                condition=WaitCondition(
                    type="event_predicate",
                    request_matcher=RequestMatcher(url_contains="/conversation"),
                    predicate=ExactDataPredicate(
                        type="exact_data",
                        value="[DONE]",
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
                            "url_contains": "/conversation",
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

    def test_playwright_output_is_streamed_into_a_bounded_buffer(self) -> None:
        async def exercise() -> Any:
            runner = SubprocessCommandRunner(max_output_bytes=200)
            return await runner.run(
                [
                    sys.executable,
                    "-c",
                    ("import sys;sys.stdout.write('x'*10000);sys.stderr.write('y'*10000)"),
                ],
                deadline=Deadline(5_000),
            )

        result = asyncio.run(exercise())
        self.assertTrue(result.truncated)
        self.assertLessEqual(
            len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8")),
            200,
        )

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_playwright_runner_timeout_terminates_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_pid_file = root / "child.pid"
            parent = root / "parent.py"
            parent.write_text(
                "\n".join(
                    [
                        "import subprocess, sys, time",
                        "from pathlib import Path",
                        "child = subprocess.Popen([",
                        "    sys.executable, '-c', 'import time; time.sleep(30)'",
                        "])",
                        f"pid_file = Path({str(child_pid_file)!r})",
                        "pid_file.write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(30)",
                    ]
                ),
                encoding="utf-8",
            )

            async def exercise() -> None:
                runner = SubprocessCommandRunner()
                with self.assertRaises(AdapterError):
                    await runner.run(
                        [sys.executable, str(parent)],
                        deadline=Deadline(500),
                        cwd=root,
                    )

            asyncio.run(exercise())
            child_pid = child_pid_file.read_text(encoding="utf-8").strip()
            listing = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    f"PID eq {child_pid}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotIn(f'"{child_pid}"', listing.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_playwright_runner_cancellation_terminates_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_pid_file = root / "child.pid"
            marker = root / "child-finished.txt"
            parent = root / "parent.py"
            child_code = (
                "import time; from pathlib import Path; "
                f"time.sleep(2); Path({str(marker)!r}).write_text('finished')"
            )
            parent.write_text(
                "\n".join(
                    [
                        "import subprocess, sys, time",
                        "from pathlib import Path",
                        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])",
                        f"pid_file = Path({str(child_pid_file)!r})",
                        "pid_file.write_text(str(child.pid), encoding='utf-8')",
                        "time.sleep(30)",
                    ]
                ),
                encoding="utf-8",
            )

            async def exercise() -> None:
                runner = SubprocessCommandRunner()
                task = asyncio.create_task(
                    runner.run(
                        [sys.executable, str(parent)],
                        deadline=Deadline(30_000),
                        cwd=root,
                    )
                )
                for _ in range(100):
                    if child_pid_file.is_file():
                        break
                    await asyncio.sleep(0.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(exercise())
            child_pid = child_pid_file.read_text(encoding="utf-8").strip()
            time.sleep(2.2)
            self.assertFalse(marker.exists())
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotIn(f'"{child_pid}"', listing.stdout)

    def test_canceling_active_side_effect_mcp_call_restarts_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            server = root / "cancel_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "import asyncio",
                        "from pathlib import Path",
                        "from mcp.server.fastmcp import FastMCP",
                        "from pydantic import BaseModel",
                        "server = FastMCP('cancel-test')",
                        "class Capture(BaseModel):",
                        "    captureId: int",
                        "class Result(BaseModel):",
                        "    capture: Capture",
                        "@server.tool(structured_output=True)",
                        "async def start_stream_capture(marker: str, delayMs: int = 0) -> Result:",
                        "    await asyncio.sleep(delayMs / 1000)",
                        "    Path(marker).write_text('started', encoding='utf-8')",
                        "    return Result(capture=Capture(captureId=31))",
                        "if __name__ == '__main__':",
                        "    server.run(transport='stdio')",
                    ]
                ),
                encoding="utf-8",
            )
            canceled_marker = root / "canceled.txt"
            restarted_marker = root / "restarted.txt"
            transport = StdioMcpToolTransport(
                command=sys.executable,
                args=[str(server)],
                cwd=root,
            )

            async def exercise() -> dict[str, Any]:
                active = asyncio.create_task(
                    transport.call_tool(
                        "start_stream_capture",
                        {"marker": str(canceled_marker), "delayMs": 2_000},
                        Deadline(10_000),
                    )
                )
                await asyncio.sleep(0.3)
                active.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await active
                await asyncio.sleep(2.1)
                result = await transport.call_tool(
                    "start_stream_capture",
                    {"marker": str(restarted_marker), "delayMs": 0},
                    Deadline(5_000),
                )
                await transport.close()
                return result

            result = asyncio.run(exercise())
            self.assertFalse(canceled_marker.exists())
            self.assertTrue(restarted_marker.is_file())
            self.assertEqual(result["capture"]["captureId"], 31)

    def test_read_only_mcp_transport_crash_restarts_on_next_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            marker = root / "crashed-once.txt"
            server = root / "crash_once_mcp_server.py"
            server.write_text(
                "\n".join(
                    [
                        "import os",
                        "from pathlib import Path",
                        "from mcp.server.fastmcp import FastMCP",
                        "from pydantic import BaseModel",
                        "server = FastMCP('crash-once-test')",
                        "class Capture(BaseModel):",
                        "    captureId: int",
                        "    captureUuid: str",
                        "class Result(BaseModel):",
                        "    capture: Capture",
                        "@server.tool(structured_output=True)",
                        "async def get_stream_status(marker: str) -> Result:",
                        "    path = Path(marker)",
                        "    if not path.exists():",
                        "        path.write_text('crashed', encoding='utf-8')",
                        "        os._exit(17)",
                        "    return Result(capture=Capture(",
                        "        captureId=42,",
                        "        captureUuid='44444444-4444-4444-8444-444444444444',",
                        "    ))",
                        "if __name__ == '__main__':",
                        "    server.run(transport='stdio')",
                    ]
                ),
                encoding="utf-8",
            )
            transport = StdioMcpToolTransport(
                command=sys.executable,
                args=[str(server)],
                cwd=root,
            )

            async def exercise() -> tuple[int, dict[str, Any]]:
                first_generation = 0
                try:
                    await transport.call_tool(
                        "get_stream_status",
                        {"marker": str(marker)},
                        Deadline(5_000),
                    )
                except BaseException as exc:
                    self.assertNotIsInstance(
                        exc,
                        (KeyboardInterrupt, SystemExit, asyncio.CancelledError),
                    )
                else:
                    self.fail("The first MCP process should have exited")
                self.assertTrue(marker.is_file())
                self.assertEqual(marker.read_text(encoding="utf-8"), "crashed")
                first_generation = transport.generation
                result = await transport.call_tool(
                    "get_stream_status",
                    {"marker": str(marker)},
                    Deadline(5_000),
                )
                second_generation = transport.generation
                await transport.close()
                self.assertGreater(second_generation, first_generation)
                return second_generation, result

            generation, result = asyncio.run(exercise())
            self.assertGreaterEqual(generation, 2)
            self.assertEqual(result["capture"]["captureId"], 42)

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
                            "url": "https://example.test/conversation/old",
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
                            "url": "https://example.test/conversation/new",
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
                request_matcher=RequestMatcher(url_contains="/conversation"),
                condition=WaitCondition(
                    type="network_finished",
                    request_matcher=RequestMatcher(url_contains="/conversation"),
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
                    "url": "https://example.test/conversation",
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
                request_matcher=RequestMatcher(url_contains="/conversation"),
                condition=WaitCondition(
                    type="event_predicate",
                    request_matcher=RequestMatcher(url_contains="/conversation"),
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
                    json={
                        "operation": "get_stream_status",
                        "payload": {
                            "experiment_id": experiment_id,
                            "capture_uuid": "11111111-1111-4111-8111-111111111111",
                        },
                    },
                )
                mismatch = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_stream_status",
                        "payload": {
                            "experiment_id": experiment_id,
                            "capture_uuid": "22222222-2222-4222-8222-222222222222",
                        },
                    },
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
                    json={
                        "operation": "get_stream_status",
                        "payload": {
                            "experiment_id": experiment_id,
                            "capture_uuid": "55555555-5555-4555-8555-555555555555",
                        },
                    },
                )
                js.generation = 6
                persisted = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_stream_status",
                        "payload": {"experiment_id": experiment_id},
                    },
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
                            "url_contains": "/conversation",
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
                                        "request_matcher": {"url_contains": "/conversation"},
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

    def test_stream_disabled_uses_not_required_collector_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client, _, _ = self.make_client(Path(temp_dir))
            request = {
                "operation": "capture_flow",
                "payload": {
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
            }
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
            self.assertEqual(manifest["collector_integrity"], "not_required")

    def test_network_evidence_window_public_inspection_and_script_queries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            request = self.capture_request()
            request["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                        "resource_types": ["fetch"],
                    },
                    "max_matches": 2,
                    "export_parts": ["all"],
                    "include_initiator": True,
                }
            ]
            request["payload"]["series"] = {
                "analysis_series_id": "series_one",
                "scenario_type": "first_message",
                "sequence_index": 1,
                "conversation_key": "conversation-local",
            }

            with client:
                self.open_session(client)
                captured = client.post("/v1/browser/run", json=request)
                self.assertEqual(captured.status_code, 200, captured.text)
                experiment_id = captured.json()["experiment_id"]

                listed = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "list_evidence",
                        "payload": {"experiment_id": experiment_id},
                    },
                )
                self.assertEqual(listed.status_code, 200, listed.text)
                network_evidence = next(
                    item
                    for item in listed.json()["result"]["evidence"]
                    if item["kind"] == "network_request"
                )
                evidence_id_value = network_evidence["evidence_id"]

                network = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_network_evidence",
                        "payload": {
                            "experiment_id": experiment_id,
                            "evidence_id": evidence_id_value,
                        },
                    },
                )
                initiator = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_request_initiator",
                        "payload": {
                            "experiment_id": experiment_id,
                            "evidence_id": evidence_id_value,
                        },
                    },
                )
                console = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "list_console_errors",
                        "payload": {"experiment_id": experiment_id},
                    },
                )
                scripts = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "search_scripts",
                        "payload": {
                            "session_id": "session_one",
                            "query": "buildConversationRequest",
                        },
                    },
                )
                source = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_script_source",
                        "payload": {
                            "session_id": "session_one",
                            "url": "https://example.test/app.js",
                            "start_line": 10,
                            "end_line": 20,
                        },
                    },
                )
                saved_source = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "save_script_source",
                        "payload": {
                            "session_id": "session_one",
                            "target_experiment_id": experiment_id,
                            "initiator_evidence_id": evidence_id_value,
                            "url": "https://example.test/app.js",
                            "start_line": 10,
                            "end_line": 20,
                            "evidence_label": "conversation-builder",
                        },
                    },
                )

            manifest = json.loads(
                (root / "experiments" / experiment_id / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["network_checkpoint"]["max_reqid"], 1)
            self.assertEqual(
                [item["reqid"] for item in manifest["network_summary"]["requests"]],
                [2],
            )
            self.assertEqual(network_evidence["request_ids"]["reqid"], 2)
            self.assertNotIn("Bearer secret", json.dumps(network_evidence))
            self.assertNotIn("session=secret", json.dumps(network_evidence))
            headers = {
                item["name"].lower(): item["value"]
                for item in network_evidence["summary"]["request_headers"]
            }
            self.assertEqual(headers["authorization"], "<redacted>")
            self.assertEqual(headers["cookie"], "<redacted>")
            all_artifact_id = next(
                item for item in network_evidence["artifact_ids"] if item.endswith("_all")
            )
            descriptor = next(
                item for item in manifest["artifacts"] if item["artifactId"] == all_artifact_id
            )
            self.assertEqual(descriptor["sensitivity"], "credential")
            self.assertTrue(descriptor["containsCredentials"])
            self.assertEqual(network.status_code, 200, network.text)
            self.assertNotIn("Bearer secret", network.text)
            self.assertEqual(initiator.status_code, 200, initiator.text)
            self.assertIn("app.js", initiator.text)
            self.assertEqual(console.status_code, 200, console.text)
            self.assertEqual(console.json()["result"]["count"], 1)
            self.assertIn("new error", console.text)
            self.assertEqual(scripts.status_code, 200, scripts.text)
            self.assertIn("buildConversationRequest", scripts.text)
            self.assertEqual(source.status_code, 200, source.text)
            self.assertIn("function buildConversationRequest", source.text)
            self.assertEqual(saved_source.status_code, 200, saved_source.text)
            saved_evidence = saved_source.json()["result"]["evidence"]
            self.assertEqual(saved_evidence["kind"], "script_source")
            self.assertEqual(saved_evidence["initiator_evidence_id"], evidence_id_value)
            self.assertEqual(len(saved_evidence["sha256"]), 64)
            saved_source_path = root / saved_evidence["artifact_paths"]["script_source"]
            self.assertIn(
                "function buildConversationRequest",
                saved_source_path.read_text(encoding="utf-8"),
            )
            evidence_kinds = {item["kind"] for item in manifest["evidence"]}
            self.assertIn("page_snapshot", evidence_kinds)
            self.assertIn("console_message", evidence_kinds)

    def test_browser_context_replay_uses_source_evidence_and_single_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            capture["payload"]["series"] = {
                "analysis_series_id": "series_replay",
                "scenario_type": "first_message",
                "sequence_index": 1,
            }
            with client:
                self.open_session(client)
                source_response = client.post("/v1/browser/run", json=capture)
                self.assertEqual(source_response.status_code, 200, source_response.text)
                source_experiment_id = source_response.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_experiment_id / "manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )

                shape = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_request_shape",
                        "payload": {
                            "experiment_id": source_experiment_id,
                            "evidence_id": source_evidence["evidence_id"],
                        },
                    },
                )
                self.assertEqual(shape.status_code, 200, shape.text)
                self.assertIn(
                    "/messages/0/id",
                    shape.json()["result"]["request_shape"]["paths"],
                )
                self.assertIsNone(shape.json()["result"]["request_body_redacted"])
                paged_shape = client.post(
                    "/v1/browser/inspect",
                    json={
                        "operation": "get_request_shape",
                        "payload": {
                            "experiment_id": source_experiment_id,
                            "evidence_id": source_evidence["evidence_id"],
                            "path_prefix": "/messages",
                            "page_idx": 0,
                            "page_size": 2,
                            "max_depth": 3,
                            "max_array_items": 1,
                            "include_redacted_body": True,
                        },
                    },
                )
                self.assertEqual(paged_shape.status_code, 200, paged_shape.text)
                paged_result = paged_shape.json()["result"]
                self.assertLessEqual(
                    len(paged_result["request_shape"]["paths"]),
                    2,
                )
                self.assertTrue(
                    all(
                        path.startswith("/messages")
                        for path in paged_result["request_shape"]["paths"]
                    )
                )
                self.assertIsInstance(
                    paged_result["request_body_redacted"],
                    list,
                )
                self.assertEqual(paged_result["pagination"]["page_size"], 2)

                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "establish a valid control replay",
                            "source_experiment_id": source_experiment_id,
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "volatile_bindings": [
                                {
                                    "binding_id": "message_id",
                                    "target": "json_pointer",
                                    "path": "/messages/0/id",
                                    "generator": "uuid4",
                                    "reuse_policy": "fresh_equivalent",
                                },
                                {
                                    "binding_id": "parent_message_id",
                                    "target": "json_pointer",
                                    "path": "/parent_message_id",
                                    "value_source": "preserve_source",
                                    "reuse_policy": "same_value",
                                },
                            ],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                            "capture": {
                                "network": True,
                                "stream": False,
                                "trace": False,
                                "screenshots": False,
                                "page_snapshots": False,
                                "console_errors": False,
                            },
                            "series": {
                                "analysis_series_id": "series_replay",
                                "scenario_type": "control_replay",
                                "predecessor_experiment_id": source_experiment_id,
                                "sequence_index": 2,
                            },
                        },
                    },
                )
                self.assertEqual(control.status_code, 200, control.text)
                self.assertEqual(control.json()["status"], "completed")
                control_experiment_id = control.json()["experiment_id"]

                replay = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control_experiment_id,
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/tracking_id",
                            },
                        },
                    },
                )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["status"], "completed")
            replay_experiment_id = replay.json()["experiment_id"]
            control_manifest = json.loads(
                (root / "experiments" / control_experiment_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            replay_manifest = json.loads(
                (root / "experiments" / replay_experiment_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                replay_manifest["replay_source"]["source_experiment_id"],
                source_experiment_id,
            )
            self.assertEqual(
                replay_manifest["replay_source"]["source_evidence_id"],
                source_evidence["evidence_id"],
            )
            self.assertEqual(control_manifest["replay_http_status"], 200)
            self.assertEqual(control_manifest["replay"]["replay_mode"], "control")
            self.assertEqual(replay_manifest["series"]["sequence_index"], 3)
            self.assertEqual(replay_manifest["execution_integrity"], "complete")
            self.assertEqual(replay_manifest["evidence_integrity"], "complete")
            self.assertNotIn("objective_integrity", replay_manifest)
            self.assertEqual(
                replay_manifest["causal_comparability"],
                "observed_equivalent",
            )
            self.assertEqual(replay_manifest["inference_eligibility"], "eligible")
            self.assertEqual(
                [item["reqid"] for item in replay_manifest["network_summary"]["requests"]],
                [4],
            )
            replay_attempt = next(
                item for item in replay_manifest["evidence"] if item["kind"] == "replay_attempt"
            )
            self.assertEqual(replay_attempt["source_evidence_id"], source_evidence["evidence_id"])
            replay_network = next(
                item for item in replay_manifest["evidence"] if item["kind"] == "network_request"
            )
            self.assertEqual(replay_network["request_ids"]["reqid"], 4)
            self.assertTrue(replay_manifest["mutation_assessment"]["mutation_effective"])
            self.assertEqual(
                replay_manifest["replay_comparison"]["control_http_status"],
                200,
            )
            spec = json.loads(
                (
                    root / "experiments" / replay_experiment_id / "replay" / "request-spec.json"
                ).read_text(encoding="utf-8")
            )
            control_spec = json.loads(
                (
                    root / "experiments" / control_experiment_id / "replay" / "request-spec.json"
                ).read_text(encoding="utf-8")
            )
            body = json.loads(spec["body"]["text"])
            control_body = json.loads(control_spec["body"]["text"])
            header_names = {item["name"].lower() for item in spec["headers"]}
            self.assertNotIn("tracking_id", body)
            self.assertEqual(
                body["messages"][0]["id"],
                replay_manifest["replay"]["current_volatile_binding_values"]["message_id"],
            )
            self.assertNotEqual(
                body["messages"][0]["id"],
                control_body["messages"][0]["id"],
            )
            self.assertEqual(
                body["parent_message_id"],
                control_body["parent_message_id"],
            )
            self.assertTrue(replay_manifest["mutation_assessment"]["non_target_fields_equivalent"])
            self.assertEqual(
                replay_manifest["pair_protocol_hash"],
                control_manifest["pair_protocol_hash"],
            )
            self.assertEqual(
                replay_manifest["pair_environment_comparison"]["status"],
                "observed_equivalent",
            )
            self.assertTrue(
                replay_manifest["pair_environment_comparison"]["observed_dimensions_equivalent"]
            )
            self.assertIn(
                "conversation_current_node",
                replay_manifest["pair_environment_comparison"]["advisory_dimensions_missing"],
            )
            self.assertNotIn("cookie", header_names)
            self.assertIn("authorization", header_names)
            diff = (
                root / "experiments" / replay_experiment_id / "replay" / "request-diff.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn("Bearer secret", diff)
            self.assertNotIn("session=secret", diff)

    def test_sse_source_automatically_enables_stream_capture_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source_response = client.post("/v1/browser/run", json=capture)
                self.assertEqual(source_response.status_code, 200, source_response.text)
                source_experiment_id = source_response.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_experiment_id / "manifest.json").read_text(
                        encoding="utf-8"
                    )
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                replay = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "control replay for an SSE source",
                            "source_experiment_id": source_experiment_id,
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                            "capture": {
                                "network": True,
                                "stream": False,
                                "trace": False,
                                "screenshots": False,
                                "page_snapshots": False,
                                "console_errors": False,
                            },
                        },
                    },
                )
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["status"], "completed")
            replay_manifest = json.loads(
                (root / "experiments" / replay.json()["experiment_id"] / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(replay_manifest["replay"]["source_is_stream"])
            self.assertTrue(replay_manifest["objective_requirements"]["require_raw_capture"])
            self.assertEqual(
                replay_manifest["replay_response_content_type"],
                "text/event-stream",
            )
            self.assertEqual(replay_manifest["execution_integrity"], "complete")
            self.assertEqual(replay_manifest["evidence_integrity"], "complete")
            self.assertEqual(
                replay_manifest["primary_integrity_dimensions"]["raw_capture"],
                "complete",
            )
            self.assertEqual(
                replay_manifest["primary_integrity_dimensions"]["artifacts"],
                "complete",
            )
            evidence_kinds = {item["kind"] for item in replay_manifest["evidence"]}
            self.assertIn("stream_request", evidence_kinds)
            self.assertIn("stream_event_range", evidence_kinds)

    def test_request_context_hash_detects_cookie_value_change_without_storing_value(
        self,
    ) -> None:
        alignment = AlignmentResult(
            status="aligned",
            playwright_page=PageState(url="https://example.test/app"),
            js_reverse_page_id="page_one",
            js_reverse_page_url="https://example.test/app",
        )
        first = {
            "url": "https://example.test/conversation",
            "requestHeadersCompleteness": "complete",
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=first; theme=dark"},
                {"name": "Authorization", "value": "Bearer first"},
            ],
        }
        second = {
            "url": "https://example.test/conversation",
            "requestHeadersCompleteness": "complete",
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=second; theme=dark"},
                {"name": "Authorization", "value": "Bearer first"},
            ],
        }

        first_fp = BrowserActionService._environment_fingerprint(
            alignment,
            first,
            phase="pre_dispatch",
        )
        second_fp = BrowserActionService._environment_fingerprint(
            alignment,
            second,
            phase="pre_dispatch",
        )
        comparison = BrowserActionService._compare_pair_environments(
            first_fp,
            second_fp,
        )
        incomplete_match = BrowserActionService._compare_pair_environments(
            first_fp,
            first_fp,
        )

        self.assertNotEqual(
            first_fp["cookie_name_value_sha256"],
            second_fp["cookie_name_value_sha256"],
        )
        self.assertNotIn("session=first", json.dumps(first_fp))
        self.assertNotIn("Bearer first", json.dumps(first_fp))
        self.assertEqual(comparison["status"], "different")
        self.assertIn("request_context_sha256", comparison["differences"])
        self.assertEqual(incomplete_match["status"], "observed_equivalent")
        self.assertTrue(incomplete_match["equivalent"])
        self.assertIn(
            "conversation_current_node",
            incomplete_match["advisory_dimensions_missing"],
        )

        unavailable = BrowserActionService._environment_fingerprint(
            alignment,
            {
                "url": "https://example.test/conversation",
                "requestHeadersArray": [{"name": "Cookie", "value": "session=first"}],
            },
            phase="pre_dispatch",
        )
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertIsNone(unavailable["request_context_sha256"])

        ordered_one = {
            "url": "https://example.test/conversation",
            "requestHeadersCompleteness": "complete",
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=one; analytics=a"},
                {"name": "Cookie", "value": "session=two"},
                {"name": "X-Request-Nonce", "value": "nonce-one"},
            ],
        }
        ordered_two = {
            **ordered_one,
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=two"},
                {"name": "Cookie", "value": "session=one; analytics=b"},
                {"name": "X-Request-Nonce", "value": "nonce-two"},
            ],
        }
        ignored_only_change = {
            **ordered_one,
            "requestHeadersArray": [
                {"name": "Cookie", "value": "session=one; analytics=changed"},
                {"name": "Cookie", "value": "session=two"},
                {"name": "X-Request-Nonce", "value": "nonce-two"},
            ],
        }
        ordered_one_hash = BrowserActionService._request_context_hashes(ordered_one)
        ordered_two_hash = BrowserActionService._request_context_hashes(ordered_two)
        ignored_one_hash = BrowserActionService._request_context_hashes(
            ordered_one,
            ignored_cookie_names=["analytics"],
            ignored_context_headers=["x-request-nonce"],
        )
        ignored_two_hash = BrowserActionService._request_context_hashes(
            ordered_two,
            ignored_cookie_names=["analytics"],
            ignored_context_headers=["x-request-nonce"],
        )
        ignored_only_change_hash = BrowserActionService._request_context_hashes(
            ignored_only_change,
            ignored_cookie_names=["analytics"],
            ignored_context_headers=["x-request-nonce"],
        )
        self.assertNotEqual(
            ordered_one_hash["cookie_name_value_sha256"],
            ordered_two_hash["cookie_name_value_sha256"],
        )
        self.assertNotEqual(
            ordered_one_hash["request_context_sha256"],
            ordered_two_hash["request_context_sha256"],
        )
        self.assertNotEqual(
            ignored_one_hash["cookie_name_value_sha256"],
            ignored_two_hash["cookie_name_value_sha256"],
        )
        self.assertEqual(
            ignored_one_hash["request_context_sha256"],
            ignored_only_change_hash["request_context_sha256"],
        )
        self.assertEqual(
            ignored_one_hash["ignored_cookie_names"],
            ["analytics"],
        )
        self.assertEqual(
            ignored_one_hash["ignored_context_headers"],
            ["x-request-nonce"],
        )

    def test_sse_terminal_contract_and_semantic_parse_affect_objective(self) -> None:
        cases = [
            {
                "name": "missing_done_marker",
                "semantic": "complete",
                "raw_only": False,
                "done": False,
                "termination": "idle_timeout",
                "expected_status": "partial",
                "expected_contract": "partial",
            },
            {
                "name": "semantic_parse_failed",
                "semantic": "failed",
                "raw_only": False,
                "done": True,
                "termination": "done_marker",
                "expected_status": "failed",
                "expected_contract": "complete",
            },
            {
                "name": "explicit_raw_only",
                "semantic": "failed",
                "raw_only": True,
                "done": True,
                "termination": "done_marker",
                "expected_status": "completed",
                "expected_contract": "complete",
            },
        ]
        for case in cases:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                client, _, js = self.make_client(
                    root,
                    include_supporting_failure=False,
                    semantic_parse_integrity=case["semantic"],
                )
                js.network_response_content_type = "text/event-stream"
                js.replay_done_marker_observed = case["done"]
                js.replay_termination_reason = case["termination"]
                capture = self.capture_request()
                capture["payload"]["capture"] = {
                    "network": True,
                    "stream": False,
                    "trace": False,
                    "screenshots": False,
                    "page_snapshots": False,
                    "console_errors": False,
                }
                capture["payload"]["requirements"] = {
                    "require_raw_capture": False,
                    "require_semantic_parse": False,
                    "require_request_snapshot": True,
                    "require_artifacts": True,
                }
                capture["payload"]["network_evidence"] = [
                    {
                        "selector_id": "conversation_submit",
                        "matcher": {
                            "url_contains": "/conversation",
                            "method": "POST",
                        },
                        "export_parts": ["all"],
                    }
                ]
                with client:
                    self.open_session(client)
                    source = client.post("/v1/browser/run", json=capture)
                    source_id = source.json()["experiment_id"]
                    source_manifest = json.loads(
                        (root / "experiments" / source_id / "manifest.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    source_evidence = next(
                        item
                        for item in source_manifest["evidence"]
                        if item["kind"] == "network_request"
                    )
                    control = client.post(
                        "/v1/browser/run",
                        json={
                            "operation": "replay_request",
                            "payload": {
                                "session_id": "session_one",
                                "objective": case["name"],
                                "source_experiment_id": source_id,
                                "source_evidence_id": source_evidence["evidence_id"],
                                "replay_mode": "control",
                                "mutations": [],
                                "terminal_conditions": [
                                    {
                                        "type": "exact_sse_data",
                                        "value": "[DONE]",
                                    }
                                ],
                                "raw_only": case["raw_only"],
                                "execution_mode": "sync",
                                "deadline_ms": 10_000,
                            },
                        },
                    )
                self.assertEqual(control.status_code, 200, control.text)
                self.assertEqual(control.json()["status"], case["expected_status"])
                manifest = json.loads(
                    (
                        root / "experiments" / control.json()["experiment_id"] / "manifest.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    manifest["stream_response_contract"]["status"],
                    case["expected_contract"],
                )
                self.assertEqual(
                    manifest["objective_requirements"]["require_semantic_parse"],
                    not case["raw_only"],
                )

    def test_environment_comparison_uses_pre_dispatch_not_final_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_id = source.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
                )
                evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "compare pre-dispatch environment",
                            "source_experiment_id": source_id,
                            "source_evidence_id": evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "verification_flow": [
                                {
                                    "step_id": "return_to_final",
                                    "action": "navigate",
                                    "value": "https://example.test/final",
                                }
                            ],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
                self.assertEqual(control.json()["status"], "completed")
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control.json()["experiment_id"],
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/tracking_id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 200, treatment.text)
            self.assertEqual(treatment.json()["status"], "completed")
            control_manifest = json.loads(
                (
                    root / "experiments" / control.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            treatment_manifest = json.loads(
                (
                    root / "experiments" / treatment.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                control_manifest["pre_dispatch_environment"]["page_url"],
                "https://example.test/app",
            )
            self.assertEqual(
                control_manifest["post_verification_environment"]["page_url"],
                "https://example.test/final",
            )
            self.assertEqual(
                treatment_manifest["pre_dispatch_environment"]["page_url"],
                "https://example.test/final",
            )
            self.assertEqual(
                treatment_manifest["post_verification_environment"]["page_url"],
                "https://example.test/final",
            )
            self.assertEqual(
                treatment_manifest["pair_environment_comparison"]["status"],
                "observed_equivalent",
            )
            self.assertIn(
                "page_url",
                treatment_manifest["pair_environment_comparison"]["advisory_differences"],
            )
            self.assertEqual(treatment_manifest["inference_eligibility"], "eligible")

    def test_setup_flow_is_inherited_and_runs_before_each_replay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, events, _ = self.make_client(root, include_supporting_failure=False)
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_id = source.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
                )
                evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "restore the same state before each replay",
                            "source_experiment_id": source_id,
                            "source_evidence_id": evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "setup_flow": [
                                {
                                    "step_id": "setup_restore",
                                    "action": "navigate",
                                    "value": "https://example.test/setup",
                                }
                            ],
                            "verification_flow": [
                                {
                                    "step_id": "verify_final",
                                    "action": "navigate",
                                    "value": "https://example.test/final",
                                }
                            ],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
                self.assertEqual(control.status_code, 200, control.text)
                self.assertEqual(control.json()["status"], "completed")
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control.json()["experiment_id"],
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/tracking_id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 200, treatment.text)
            self.assertEqual(treatment.json()["status"], "completed")
            control_manifest = json.loads(
                (
                    root / "experiments" / control.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            treatment_manifest = json.loads(
                (
                    root / "experiments" / treatment.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                control_manifest["pre_dispatch_environment"]["page_url"],
                "https://example.test/setup",
            )
            self.assertEqual(
                treatment_manifest["pre_dispatch_environment"]["page_url"],
                "https://example.test/setup",
            )
            self.assertNotIn(
                "page_url",
                treatment_manifest["pair_environment_comparison"]["differences"],
            )
            self.assertEqual(
                control_manifest["replay"]["pair_protocol"]["setup_flow"][0]["step_id"],
                "setup_restore",
            )
            setup_indices = [
                index
                for index, item in enumerate(events)
                if item == "playwright.step:setup_restore"
            ]
            replay_indices = [index for index, item in enumerate(events) if item == "js.replay"]
            verify_indices = [
                index for index, item in enumerate(events) if item == "playwright.step:verify_final"
            ]
            self.assertEqual(len(setup_indices), 2)
            self.assertEqual(len(replay_indices), 2)
            self.assertEqual(len(verify_indices), 2)
            for setup_index, replay_index, verify_index in zip(
                setup_indices,
                replay_indices,
                verify_indices,
                strict=True,
            ):
                self.assertLess(setup_index, replay_index)
                self.assertLess(replay_index, verify_index)

    def test_treatment_fails_when_mutation_is_not_observed_on_wire(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_id = source.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "control",
                            "source_experiment_id": source_id,
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                            "capture": {
                                "network": True,
                                "stream": False,
                                "trace": False,
                                "screenshots": False,
                                "page_snapshots": False,
                                "console_errors": False,
                            },
                        },
                    },
                )
                self.assertEqual(control.json()["status"], "completed")
                js.ignore_replay_spec_for_reqids.add(4)
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control.json()["experiment_id"],
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/tracking_id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 200, treatment.text)
            self.assertEqual(treatment.json()["status"], "failed")
            manifest = json.loads(
                (
                    root / "experiments" / treatment.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["mutation_assessment"]["mutation_effective"])
            self.assertEqual(manifest["evidence_integrity"], "failed")

    def test_sse_treatment_json_rejection_remains_protocol_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_id = source.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "SSE control",
                            "source_experiment_id": source_id,
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
                self.assertEqual(control.json()["status"], "completed")
                js.replay_response_status = 422
                js.network_response_content_type = "application/json"
                js.replay_body_preview = json.dumps({"missing": ["messages[0].id"]})
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control.json()["experiment_id"],
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/messages/0/id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 200, treatment.text)
            self.assertEqual(treatment.json()["status"], "completed")
            manifest = json.loads(
                (
                    root / "experiments" / treatment.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["protocol_rejection_observed"])
            self.assertEqual(manifest["replay_http_status"], 422)
            self.assertEqual(
                manifest["primary_integrity_dimensions"]["raw_capture"],
                "not_applicable_non_stream_response",
            )
            self.assertEqual(manifest["execution_integrity"], "complete")
            self.assertEqual(manifest["evidence_integrity"], "complete")
            self.assertNotIn("objective_integrity", manifest)
            self.assertIn(
                "field_required",
                manifest["replay_response_classification"]["inference_hints"],
            )

    def test_control_fails_when_volatile_binding_is_not_observed_on_wire(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_id = source.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                js.ignore_replay_spec_for_reqids.add(3)
                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "control binding must reach the wire",
                            "source_experiment_id": source_id,
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "volatile_bindings": [
                                {
                                    "binding_id": "message_id",
                                    "target": "json_pointer",
                                    "path": "/messages/0/id",
                                    "generator": "uuid4",
                                }
                            ],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(control.status_code, 200, control.text)
            self.assertEqual(control.json()["status"], "failed")
            manifest = json.loads(
                (
                    root / "experiments" / control.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(manifest["mutation_assessment"]["volatile_bindings_effective"])
            self.assertIn("volatile bindings", " ".join(manifest["errors"]).lower())

    def test_control_rejects_unexpected_redirect_response_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_id = source.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                js.replay_redirected = True
                js.replay_final_url = "https://example.test/login"
                js.network_response_content_type = "text/html"
                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "redirected login must not be a valid control",
                            "source_experiment_id": source_id,
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(control.status_code, 200, control.text)
            self.assertEqual(control.json()["status"], "failed")
            manifest = json.loads(
                (
                    root / "experiments" / control.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["replay_response_classification"]["classification"],
                "unexpected_redirect",
            )

    def test_treatment_rejects_tampered_pair_protocol_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                _, _, control_id, control_manifest = self.capture_source_and_control(
                    client,
                    root,
                )
                control_manifest["replay"]["pair_protocol"]["capture"]["network"] = False
                (root / "experiments" / control_id / "manifest.json").write_text(
                    json.dumps(control_manifest), encoding="utf-8"
                )
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control_id,
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/tracking_id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 409, treatment.text)
            self.assertEqual(
                treatment.json()["detail"]["error"]["code"],
                "control_pair_protocol_invalid",
            )

    def test_treatment_rejects_legacy_objective_integrity_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                _, _, control_id, control_manifest = self.capture_source_and_control(
                    client,
                    root,
                )
                control_manifest.pop("execution_integrity")
                control_manifest.pop("evidence_integrity")
                control_manifest["objective_integrity"] = "complete"
                (root / "experiments" / control_id / "manifest.json").write_text(
                    json.dumps(control_manifest), encoding="utf-8"
                )
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control_id,
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/tracking_id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 409, treatment.text)
            self.assertEqual(
                treatment.json()["detail"]["error"]["code"],
                "control_replay_not_usable",
            )

    def test_replay_request_candidate_ambiguity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                _, _, control_id, _ = self.capture_source_and_control(client, root)
                js.duplicate_next_replay_requests = 1
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control_id,
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/tracking_id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 200, treatment.text)
            self.assertEqual(treatment.json()["status"], "failed")
            manifest = json.loads(
                (
                    root / "experiments" / treatment.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("ambiguous", " ".join(manifest["errors"]).lower())
            self.assertIsNone(manifest["replay"]["network_evidence_id"])

    def test_replay_request_without_observed_timestamp_fails_correlation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            with client:
                self.open_session(client)
                _, _, control_id, _ = self.capture_source_and_control(client, root)
                js.omit_observed_at_reqids.add(4)
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control_id,
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/tracking_id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 200, treatment.text)
            self.assertEqual(treatment.json()["status"], "failed")
            manifest = json.loads(
                (
                    root / "experiments" / treatment.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn(
                "no replay request matched",
                " ".join(manifest["errors"]).lower(),
            )
            self.assertIsNone(manifest["replay"]["network_evidence_id"])

    def test_replay_correlation_ignores_background_get_and_other_query(self) -> None:
        expected_hash = "body-hash"
        replay_plan = {
            "expected_request_body_canonical_sha256": expected_hash,
            "spec": {
                "method": "POST",
                "url": "https://example.test/api/items?cursor=expected",
            },
            "dispatch_wall_time_ms": 1_000,
            "correlation_window_end_wall_time_ms": 2_000,
        }
        entries = [
            {
                "evidence_id": "background_get",
                "kind": "network_request",
                "selector_id": "replay_request",
                "summary": {
                    "method": "GET",
                    "url": "https://example.test/api/items?cursor=expected",
                },
                "request_body_canonical_sha256": expected_hash,
                "observed_at": 1_100,
            },
            {
                "evidence_id": "other_query",
                "kind": "network_request",
                "selector_id": "replay_request",
                "summary": {
                    "method": "POST",
                    "url": "https://example.test/api/items?cursor=other",
                },
                "request_body_canonical_sha256": expected_hash,
                "observed_at": 1_200,
            },
            {
                "evidence_id": "expected",
                "kind": "network_request",
                "selector_id": "replay_request",
                "summary": {
                    "method": "POST",
                    "url": "https://example.test/api/items?cursor=expected",
                },
                "request_body_canonical_sha256": expected_hash,
                "observed_at": 1_300,
            },
        ]

        selected, error = BrowserActionService._select_replay_network_evidence(
            entries,
            replay_plan,
        )

        self.assertIsNone(error)
        self.assertEqual(selected["evidence_id"], "expected")

    def test_preview_only_validation_error_cannot_prove_required(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_id = source.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "SSE control for exact response test",
                            "source_experiment_id": source_id,
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
                self.assertEqual(control.json()["status"], "completed")
                js.replay_response_status = 422
                js.network_response_content_type = "application/json"
                js.replay_body_preview = json.dumps({"missing": ["messages[0].id"]})
                js.response_body_available = False
                treatment = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "replay_mode": "treatment",
                            "control_experiment_id": control.json()["experiment_id"],
                            "mutation": {
                                "type": "remove_json_path",
                                "path": "/messages/0/id",
                            },
                        },
                    },
                )
            self.assertEqual(treatment.status_code, 200, treatment.text)
            self.assertEqual(treatment.json()["status"], "partial")
            manifest = json.loads(
                (
                    root / "experiments" / treatment.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            classification = manifest["replay_response_classification"]
            self.assertEqual(classification["classification"], "validation_rejection")
            self.assertFalse(classification["evidence_sufficient"])
            self.assertEqual(
                manifest["response_evidence_source"],
                "replay_preview_fallback",
            )
            self.assertTrue(manifest["protocol_rejection_observed"])
            self.assertEqual(
                manifest["primary_integrity_dimensions"]["request_snapshot"],
                "complete",
            )
            replay_network = next(
                item
                for item in manifest["evidence"]
                if item.get("evidence_id") == manifest["replay"]["network_evidence_id"]
            )
            self.assertEqual(
                replay_network["summary"]["snapshot_integrity"]["response_body_completeness"],
                "partial",
            )

    def test_non_validation_rejections_are_inconclusive(self) -> None:
        cases = [
            (401, '{"error":"login required"}', "authentication_failure"),
            (429, '{"error":"rate limited"}', "rate_limited"),
            (500, '{"error":"server unavailable"}', "server_failure"),
            (422, '{"error":"invalid request"}', "unknown_rejection"),
        ]
        for status, preview, expected in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                client, _, js = self.make_client(
                    root,
                    include_supporting_failure=False,
                )
                with client:
                    self.open_session(client)
                    _, _, control_id, _ = self.capture_source_and_control(client, root)
                    js.replay_response_status = status
                    js.replay_body_preview = preview
                    treatment = client.post(
                        "/v1/browser/run",
                        json={
                            "operation": "replay_request",
                            "payload": {
                                "replay_mode": "treatment",
                                "control_experiment_id": control_id,
                                "mutation": {
                                    "type": "remove_json_path",
                                    "path": "/tracking_id",
                                },
                            },
                        },
                    )
                self.assertEqual(treatment.status_code, 200, treatment.text)
                self.assertEqual(treatment.json()["status"], "partial")
                manifest = json.loads(
                    (
                        root / "experiments" / treatment.json()["experiment_id"] / "manifest.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertTrue(manifest["mutation_assessment"]["mutation_effective"])
                self.assertFalse(manifest["protocol_rejection_observed"])
                self.assertEqual(
                    manifest["replay_response_classification"]["classification"],
                    expected,
                )

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
                "summary": {"url": "https://example.test/conversation", "method": "POST"},
            },
            {
                "evidence_id": "ev_two",
                "kind": "network_request",
                "request_ids": {
                    "network_request_id": "network-two",
                    "collector_generation": 7,
                    "cdp_request_id": "cdp-two",
                },
                "summary": {"url": "https://example.test/conversation", "method": "POST"},
            },
        ]
        matched, association = BrowserActionService._associate_stream_network_evidence(
            {
                "networkRequestId": "network-two",
                "collectorGeneration": 7,
                "cdpRequestId": "cdp-two",
                "url": "https://example.test/conversation",
                "method": "POST",
            },
            entries,
        )
        ambiguous, fallback = BrowserActionService._associate_stream_network_evidence(
            {
                "url": "https://example.test/conversation",
                "method": "POST",
            },
            entries,
        )

        self.assertEqual(matched["evidence_id"], "ev_two")
        self.assertEqual(
            association["method"],
            "network_request_id_and_generation",
        )
        self.assertIsNone(ambiguous)
        self.assertEqual(fallback["status"], "ambiguous")
        self.assertEqual(fallback["candidate_count"], 2)

    def test_network_snapshot_does_not_upgrade_stream_artifact_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(
                root,
                include_supporting_failure=False,
                artifact_integrity="failed",
            )
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
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
            self.assertEqual(
                stream["summary"]["network_snapshot_integrity"],
                "complete",
            )
            self.assertEqual(
                manifest["primary_integrity_dimensions"]["artifacts"],
                "failed",
            )

    def test_replay_primary_stream_is_locked_to_exact_network_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = "text/event-stream"
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {
                        "url_contains": "/conversation",
                        "method": "POST",
                    },
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                self.assertEqual(source.status_code, 200, source.text)
                source_id = source.json()["experiment_id"]
                source_manifest = json.loads(
                    (root / "experiments" / source_id / "manifest.json").read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                js.extra_same_endpoint_stream = {
                    "cdpRequestId": "other-cdp",
                    "persistentRequestId": "other-persistent",
                    "networkRequestId": "network-other",
                    "collectorGeneration": 1,
                    "url": "https://example.test/conversation",
                    "method": "POST",
                    "resourceType": "fetch",
                    "status": "failed",
                    "terminalReason": "failed",
                    "integrityStatus": "failed",
                    "rawCaptureIntegrity": "failed",
                    "semanticParseIntegrity": "failed",
                    "requestSnapshotIntegrity": "failed",
                    "artifactIntegrity": "failed",
                    "responseObserved": True,
                    "defaultDoneMarkerObserved": False,
                    "rawEventCount": 0,
                    "semanticEventCount": 0,
                    "primaryEventSource": "none",
                    "coreArtifacts": [],
                }
                control = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "lock replay primary to exact stream",
                            "source_experiment_id": source_id,
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )
            self.assertEqual(control.status_code, 200, control.text)
            self.assertEqual(control.json()["status"], "completed")
            manifest = json.loads(
                (
                    root / "experiments" / control.json()["experiment_id"] / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["primary_requests"]), 1)
            self.assertEqual(
                manifest["primary_requests"][0]["networkEvidenceId"],
                manifest["replay"]["network_evidence_id"],
            )
            self.assertEqual(manifest["primary_request_integrity"], "complete")
            supporting = [
                item
                for item in manifest["stream_status"]["requests"]
                if item.get("networkRequestId") == "network-other"
            ]
            self.assertEqual(len(supporting), 1)
            self.assertEqual(supporting[0]["integrityStatus"], "failed")

    def test_runtime_replay_reader_handles_cr_eof_and_exact_byte_limit(self) -> None:
        source = Path("src/skill_temple/browser_adapters.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        function_source: str | None = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if node.name != "evaluate_browser_replay":
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Assign):
                    continue
                if not any(
                    isinstance(target, ast.Name) and target.id == "function"
                    for target in child.targets
                ):
                    continue
                function_source = ast.literal_eval(child.value)
                break
        self.assertIsNotNone(function_source)
        script = f"""
const replay = {function_source};
class TestHeaders {{
  constructor() {{ this.values = []; }}
  append(name, value) {{ this.values.push([name, value]); }}
}}
globalThis.Headers = TestHeaders;
async function runCase(chunks, responseControl, contentType = 'text/event-stream') {{
  let index = 0;
  globalThis.fetch = async () => ({{
    status: 200,
    statusText: 'OK',
    url: 'https://example.test/conversation',
    redirected: false,
    ok: true,
    headers: {{entries: () => [['content-type', contentType]][Symbol.iterator]()}},
    body: {{getReader: () => ({{
      read: async () => index < chunks.length
        ? {{done: false, value: chunks[index++]}}
        : {{done: true, value: undefined}},
      cancel: async () => undefined,
    }})}},
  }});
  return replay({{localFile: {{text: JSON.stringify({{
    url: 'https://example.test/conversation',
    method: 'POST',
    headers: [],
    body: null,
    responseControl,
  }})}}}});
}}
(async () => {{
  const crOnly = await runCase(
    [new Uint8Array(Buffer.from('data: first\\r\\rdata: [DONE]'))],
    {{maxResponseBytes: 8192, idleTimeoutMs: 1000, doneMarker: '[DONE]', doneEventName: null}},
  );
  const exactLimit = await runCase(
    [new Uint8Array(8192).fill(97)],
    {{maxResponseBytes: 8192, idleTimeoutMs: 1000, doneMarker: null, doneEventName: null}},
  );
  const ndjson = await runCase(
    [
      new Uint8Array(Buffer.from('{{"id":1}}\\n{{"id":')),
      new Uint8Array(Buffer.from('2}}\\nnot-json\\n')),
    ],
    {{
      maxResponseBytes: 8192,
      idleTimeoutMs: 1000,
      responseMode: 'ndjson',
      terminalConditions: [{{type: 'network_close'}}],
    }},
    'application/x-ndjson',
  );
  const rawStream = await runCase(
    [
      new Uint8Array(Buffer.from('abc')),
      new Uint8Array(Buffer.from('defg')),
    ],
    {{
      maxResponseBytes: 8192,
      idleTimeoutMs: 1000,
      responseMode: 'raw_stream',
      terminalConditions: [{{type: 'network_close'}}],
    }},
    'application/octet-stream',
  );
  console.log(JSON.stringify({{crOnly, exactLimit, ndjson, rawStream}}));
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
        with tempfile.TemporaryDirectory() as temp_dir:
            script_path = Path(temp_dir) / "replay-reader-gate.js"
            script_path.write_text(script, encoding="utf-8")
            try:
                result = subprocess.run(
                    ["node", str(script_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
            except FileNotFoundError:
                self.skipTest("Node.js is required for the runtime replay reader gate")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["crOnly"]["doneMarkerObserved"])
        self.assertEqual(payload["crOnly"]["terminationReason"], "done_marker")
        self.assertFalse(payload["crOnly"]["truncated"])
        self.assertEqual(payload["exactLimit"]["bodyByteLength"], 8192)
        self.assertFalse(payload["exactLimit"]["truncated"])
        self.assertEqual(
            payload["exactLimit"]["terminationReason"],
            "network_close",
        )
        self.assertEqual(payload["ndjson"]["responseMode"], "ndjson")
        self.assertEqual(payload["ndjson"]["ndjsonRecordCount"], 3)
        self.assertEqual(payload["ndjson"]["ndjsonParseErrorCount"], 1)
        self.assertEqual(
            [item["valid"] for item in payload["ndjson"]["ndjsonRecordMetadata"]],
            [True, True, False],
        )
        self.assertEqual(len(payload["ndjson"]["chunkBoundaries"]), 2)
        self.assertEqual(payload["rawStream"]["responseMode"], "raw_stream")
        self.assertEqual(payload["rawStream"]["bodyByteLength"], 7)
        self.assertEqual(
            [item["byteLength"] for item in payload["rawStream"]["chunkBoundaries"]],
            [3, 4],
        )
        self.assertEqual(payload["rawStream"]["terminationReason"], "network_close")

    def test_replay_source_without_content_type_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.network_response_content_type = None
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {"url_contains": "/conversation", "method": "POST"},
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_manifest = json.loads(
                    (
                        root / "experiments" / source.json()["experiment_id"] / "manifest.json"
                    ).read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                replay = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "replay a response without content type",
                            "source_experiment_id": source.json()["experiment_id"],
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )

            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["status"], "completed")
            manifest = json.loads(
                (root / "experiments" / replay.json()["experiment_id"] / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIsNone(manifest["replay"]["source_content_type"])
            self.assertIsNone(manifest["replay_response_content_type"])
            self.assertEqual(
                manifest["replay_response_classification"]["classification"],
                "success",
            )

    def test_exploratory_replay_supports_multiple_add_and_remove_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, _ = self.make_client(root, include_supporting_failure=False)
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {"url_contains": "/conversation", "method": "POST"},
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_manifest = json.loads(
                    (
                        root / "experiments" / source.json()["experiment_id"] / "manifest.json"
                    ).read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                replay = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "explore multiple structural mutations",
                            "source_experiment_id": source.json()["experiment_id"],
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "exploratory",
                            "mutations": [
                                {
                                    "type": "remove_json_path",
                                    "path": "/tracking_id",
                                },
                                {
                                    "type": "add_json_path",
                                    "path": "/experimental_flag",
                                    "value": True,
                                },
                            ],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )

            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["status"], "completed")
            experiment_id = replay.json()["experiment_id"]
            manifest = json.loads(
                (root / "experiments" / experiment_id / "manifest.json").read_text(encoding="utf-8")
            )
            spec = json.loads(
                (root / "experiments" / experiment_id / "replay" / "request-spec.json").read_text(
                    encoding="utf-8"
                )
            )
            body = json.loads(spec["body"]["text"])
            self.assertNotIn("tracking_id", body)
            self.assertTrue(body["experimental_flag"])
            self.assertEqual(manifest["replay"]["replay_mode"], "exploratory")
            self.assertEqual(manifest["inference_eligibility"], "ineligible")
            self.assertTrue(manifest["mutation_assessment"]["all_mutations_effective"])

    def test_setup_network_response_output_is_injected_into_replay_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client, _, js = self.make_client(root, include_supporting_failure=False)
            js.setup_output_response = {"conversation": {"id": "created-conversation-id"}}
            capture = self.capture_request()
            capture["payload"]["network_evidence"] = [
                {
                    "selector_id": "conversation_submit",
                    "matcher": {"url_contains": "/conversation", "method": "POST"},
                    "export_parts": ["all"],
                }
            ]
            with client:
                self.open_session(client)
                source = client.post("/v1/browser/run", json=capture)
                source_manifest = json.loads(
                    (
                        root / "experiments" / source.json()["experiment_id"] / "manifest.json"
                    ).read_text(encoding="utf-8")
                )
                source_evidence = next(
                    item
                    for item in source_manifest["evidence"]
                    if item["kind"] == "network_request"
                )
                replay = client.post(
                    "/v1/browser/run",
                    json={
                        "operation": "replay_request",
                        "payload": {
                            "session_id": "session_one",
                            "objective": "create a conversation and inject its id",
                            "source_experiment_id": source.json()["experiment_id"],
                            "source_evidence_id": source_evidence["evidence_id"],
                            "replay_mode": "control",
                            "mutations": [],
                            "setup_flow": [
                                {
                                    "step_id": "setup_create",
                                    "action": "navigate",
                                    "value": "https://example.test/create",
                                }
                            ],
                            "setup_outputs": [
                                {
                                    "binding_id": "conversation_id",
                                    "source": "network_response_json",
                                    "selector": {
                                        "url_contains": "/api/conversations",
                                        "method": "POST",
                                    },
                                    "pointer": "/conversation/id",
                                }
                            ],
                            "volatile_bindings": [
                                {
                                    "binding_id": "conversation_id",
                                    "target": "json_pointer",
                                    "path": "/parent_message_id",
                                    "value_source": "setup_output",
                                    "reuse_policy": "fresh_equivalent",
                                }
                            ],
                            "execution_mode": "sync",
                            "deadline_ms": 10_000,
                        },
                    },
                )

            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["status"], "completed")
            experiment_id = replay.json()["experiment_id"]
            manifest = json.loads(
                (root / "experiments" / experiment_id / "manifest.json").read_text(encoding="utf-8")
            )
            spec = json.loads(
                (root / "experiments" / experiment_id / "replay" / "request-spec.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                json.loads(spec["body"]["text"])["parent_message_id"],
                "created-conversation-id",
            )
            self.assertEqual(
                manifest["replay"]["current_volatile_binding_values"]["conversation_id"],
                "created-conversation-id",
            )
            self.assertEqual(
                manifest["replay"]["setup_output_bindings"][0]["binding_id"],
                "conversation_id",
            )
            self.assertIn(
                "request_url_sha256",
                manifest["replay"]["setup_output_bindings"][0],
            )
            self.assertNotIn(
                "request_url",
                manifest["replay"]["setup_output_bindings"][0],
            )

    def test_stop_sequence_accepts_finished_or_timeout_observation(self) -> None:
        for condition in [
            {
                "type": "network_finished",
                "request_matcher": {"url_contains": "/conversation"},
            },
            {"type": "timeout", "timeout_ms": 1_000},
        ]:
            request = {
                "operation": "capture_flow",
                "payload": {
                    "session_id": "session_one",
                    "objective": "observe Stop without assuming cancel",
                    "primary_request": {"expected_min_matches": 0},
                    "flow": [
                        {
                            "step_id": "wait_started",
                            "action": "wait",
                            "condition": {
                                "type": "first_event",
                                "request_matcher": {"url_contains": "/conversation"},
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
            }
            with self.subTest(condition=condition["type"]):
                parsed = CaptureFlowRequest.model_validate(request)
                self.assertEqual(parsed.payload.flow[-1].condition.type, condition["type"])

    def test_action_experiment_summary_is_bounded(self) -> None:
        manifest = {
            "experiment_id": "exp_many",
            "session_id": "session_many",
            "status": "completed",
            "execution_integrity": "complete",
            "evidence_integrity": "complete",
            "primary_requests": [
                {
                    "cdpRequestId": f"request-{index}",
                    "url": "https://example.test/" + ("x" * 5_000),
                    "method": "POST",
                    "status": "finished",
                    "coreArtifacts": [{"large": "y" * 10_000}],
                }
                for index in range(500)
            ],
            "network_summary": {"requests": ["large"] * 10_000},
            "artifacts": [{"payload": "z" * 10_000}] * 100,
            "warnings": ["w" * 2_000] * 100,
            "errors": [],
        }
        summary = BrowserActionService._experiment_summary(manifest)
        self.assertEqual(summary["primary_request_count"], 500)
        self.assertEqual(len(summary["primary_requests"]), 10)
        self.assertNotIn("network_summary", summary)
        self.assertNotIn("artifacts", summary)
        self.assertLess(len(json.dumps(summary).encode("utf-8")), 50_000)

    def test_playwright_locator_rendering_uses_supported_cli_locators(self) -> None:
        from skill_temple.browser_adapters import PlaywrightCliAdapter

        adapter = PlaywrightCliAdapter()
        self.assertEqual(adapter.render_locator(Locator(ref="e12")), "e12")
        self.assertEqual(adapter.render_locator(Locator(css="#send")), "#send")
        self.assertEqual(
            adapter.render_locator(Locator(role="button", name="Send")),
            'getByRole("button", { name: "Send" })',
        )
        self.assertEqual(
            adapter.render_locator(Locator(placeholder="Message")),
            'getByPlaceholder("Message")',
        )

    def test_playwright_attach_argv_matches_stage0_verified_format(self) -> None:
        self.assertEqual(
            build_playwright_attach_args(
                "http://127.0.0.1:9222",
                "session_one",
            ),
            [
                "attach",
                "--cdp",
                "http://127.0.0.1:9222",
                "--session",
                "session_one",
            ],
        )


if __name__ == "__main__":
    unittest.main()
