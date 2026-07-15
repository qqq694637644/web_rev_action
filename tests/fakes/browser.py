from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from skill_temple.browser.adapters.contracts import (
    AlignmentResult,
    PageState,
    StreamCheckpoint,
    StreamRequestCheckpoint,
    StreamWaitResult,
)
from skill_temple.browser_models import (
    FlowStep,
    RequestMatcher,
    WaitCondition,
)
from skill_temple.browser_service import (
    Deadline,
)


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
        self.replay_done_event_name_observed: str | None = "complete"
        self.replay_truncated = False
        self.replay_observed_response_mode: str | None = None
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
                    "eventName": "chunk",
                    "data": "fixture-complete",
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
            "url": "https://example.test/api/resource",
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
                    "url": "https://example.test/api/resource",
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
                    "url": "https://example.test/api/setup-resource",
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
                    "url": "https://example.test/api/resource",
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
                        "records": [
                            {
                                "id": "source-record-id",
                                "source": {"kind": "client"},
                                "content": {"segments": ["hello"]},
                            }
                        ],
                        "cursor_id": "source-cursor-id",
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
                else "https://example.test/api/setup-resource"
                if reqid == 50
                else "https://example.test/api/resource?tracking=abc"
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
                    "lineContent": "buildResourceRequest(payload)",
                }
            ],
        }

    async def get_script_source(
        self,
        deadline: Deadline,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return {"source": "function buildResourceRequest(payload) { return payload; }"}

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
        response_mode = self.replay_observed_response_mode or (
            "sse" if is_stream else "ordinary"
        )
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
