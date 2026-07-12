from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from toolchain_validation import (
    PLAYWRIGHT_PACKAGE,
    build_npx_command,
    find_free_port,
    start_chrome,
    stop_process,
    wait_for_http,
)
from toolchain_validation_server import SSE_EVENTS, start_server

from skill_temple.browser_adapters import (
    JsReverseMcpAdapter,
    PlaywrightCliAdapter,
    StdioMcpToolTransport,
)
from skill_temple.browser_models import (
    CancelExperimentRequest,
    CaptureFlowRequest,
    CloseSessionRequest,
    GetRequestShapeRequest,
    OpenSessionRequest,
    ReplayRequestRequest,
)
from skill_temple.browser_service import BrowserActionService, ExperimentStore
from skill_temple.workspace_models import (
    WorkspaceExecPwshRequest,
    WorkspaceInspectRequest,
    WorkspaceReadFilesRequest,
)
from skill_temple.workspace_service import AnalysisWorkspaceService

SESSION_ID = "browser-action-windows-smoke"


def artifact_by_kind(manifest: dict[str, Any], *kinds: str) -> dict[str, Any]:
    artifacts = [
        item for item in manifest.get("artifacts", []) if isinstance(item, dict)
    ]
    for kind in kinds:
        for item in artifacts:
            if item.get("kind") == kind:
                return item
    raise AssertionError(f"Missing artifact kinds {kinds}: {manifest.get('artifacts')}")


def relative_path(descriptor: dict[str, Any]) -> str:
    value = descriptor.get("relativePath") or descriptor.get("relative_path")
    if not isinstance(value, str):
        raise AssertionError(f"Artifact has no relative path: {descriptor}")
    return value


def find_numeric_status(value: Any) -> int | None:
    if isinstance(value, dict):
        status = value.get("status")
        if isinstance(status, int):
            return status
        for child in value.values():
            found = find_numeric_status(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_numeric_status(child)
            if found is not None:
                return found
    elif isinstance(value, str):
        try:
            return find_numeric_status(json.loads(value))
        except json.JSONDecodeError:
            return None
    return None


def process_matches(patterns: list[str], excluded_pid: int) -> list[str]:
    escaped = [item.replace("'", "''") for item in patterns]
    pattern_array = ",".join(f"'{item}'" for item in escaped)
    script = f"""
$patterns = @({pattern_array})
Get-CimInstance Win32_Process |
  Where-Object {{
    $process = $_
    $matched = $false
    foreach ($pattern in $patterns) {{
      if ($pattern -and $process.CommandLine -and
          $process.CommandLine.ToLowerInvariant().Contains($pattern.ToLowerInvariant())) {{
        $matched = $true
      }}
    }}
    $process.ProcessId -ne {excluded_pid} -and
    $process.Name -notmatch '^pwsh' -and
    $process.CommandLine -notmatch 'browser_action_smoke.py' -and
    $matched
  }} |
  ForEach-Object {{ "$($_.ProcessId)|$($_.Name)|$($_.CommandLine)" }}
"""
    completed = subprocess.run(
        ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return [line for line in completed.stdout.splitlines() if line.strip()]


async def run_smoke(repo_root: Path, js_reverse_entry: Path) -> dict[str, Any]:
    fixture_root = repo_root / "tests" / "fixtures" / "toolchain_validation"
    analysis_root = repo_root / "data" / "analysis-workspace"
    analysis_root.mkdir(parents=True, exist_ok=True)
    evidence_root = Path(
        tempfile.mkdtemp(prefix="browser-action-smoke-", dir=analysis_root)
    ).resolve()
    chrome_profile = Path(tempfile.mkdtemp(prefix="browser-action-smoke-chrome-"))
    server, server_thread = start_server(fixture_root)
    fixture_port = int(server.server_address[1])
    fixture_url = f"http://127.0.0.1:{fixture_port}/"
    cdp_port = find_free_port()
    endpoint = f"http://127.0.0.1:{cdp_port}"
    chrome_process: subprocess.Popen[bytes] | None = None
    service: BrowserActionService | None = None
    closed = False
    try:
        chrome_process = start_chrome(cdp_port, chrome_profile)
        wait_for_http(f"{endpoint}/json/version", timeout=20)
        node = shutil.which("node")
        if node is None:
            raise RuntimeError("node was not found on PATH")
        playwright = PlaywrightCliAdapter(
            command_prefix=build_npx_command(PLAYWRIGHT_PACKAGE, []),
            cwd=evidence_root,
        )
        transport = StdioMcpToolTransport(
            command=node,
            args=[
                str(js_reverse_entry),
                "--browserUrl",
                endpoint,
                "--allowedRoots",
                str(evidence_root),
                "--streamArtifactRoot",
                "0",
            ],
            cwd=evidence_root,
        )
        service = BrowserActionService(
            playwright=playwright,
            js_reverse=JsReverseMcpAdapter(transport),
            experiments=ExperimentStore(evidence_root),
            default_browser_endpoint=endpoint,
            private_mcp_browser_endpoint=endpoint,
            require_private_mcp_endpoint=True,
        )
        opened = await service.run(
            OpenSessionRequest(
                operation="open_session",
                payload={
                    "session_id": SESSION_ID,
                    "browser_endpoint": endpoint,
                },
            )
        )
        if opened.status != "completed":
            raise AssertionError(f"open_session failed: {opened.model_dump()}")
        captured = await service.run(
            CaptureFlowRequest(
                operation="capture_flow",
                payload={
                    "session_id": SESSION_ID,
                    "objective": "real Windows browser action SSE smoke",
                    "primary_request": {
                        "url_contains": "/api/sse",
                        "method": "GET",
                        "resource_types": ["eventsource"],
                        "mime_types": ["text/event-stream"],
                        "expected_min_matches": 1,
                        "expected_max_matches": 2,
                        "allow_supporting_failures": True,
                        "include_in_flight": False,
                    },
                    "flow": [
                        {
                            "step_id": "navigate_fixture",
                            "action": "navigate",
                            "value": fixture_url,
                            "timeout_ms": 15_000,
                        },
                        {
                            "step_id": "run_capture_one",
                            "action": "click",
                            "locator": {"css": "#run-capture"},
                            "timeout_ms": 10_000,
                        },
                        {
                            "step_id": "wait_capture_one_done",
                            "action": "wait",
                            "condition": {
                                "type": "event_predicate",
                                "request_matcher": {
                                    "url_contains": "/api/sse",
                                    "method": "GET",
                                    "resource_types": ["eventsource"],
                                    "mime_types": ["text/event-stream"],
                                },
                                "predicate": {
                                    "type": "exact_data",
                                    "value": "[DONE]",
                                },
                                "timeout_ms": 15_000,
                            },
                        },
                        {
                            "step_id": "run_capture_two",
                            "action": "click",
                            "locator": {"css": "#run-capture"},
                            "timeout_ms": 10_000,
                        },
                    ],
                    "wait_for": {
                        "type": "event_predicate",
                        "request_matcher": {
                            "url_contains": "/api/sse",
                            "method": "GET",
                            "resource_types": ["eventsource"],
                            "mime_types": ["text/event-stream"],
                        },
                        "predicate": {"type": "exact_data", "value": "[DONE]"},
                        "timeout_ms": 15_000,
                    },
                    "execution_mode": "sync",
                    "deadline_ms": 42_000,
                    "capture": {
                        "network": True,
                        "stream": True,
                        "trace": True,
                        "screenshots": True,
                        "page_snapshots": True,
                        "console_errors": True,
                    },
                },
            )
        )
        if captured.status != "completed":
            raise AssertionError(f"capture_flow failed: {captured.model_dump()}")
        manifest_relative = str(captured.result["manifest_relative_path"])
        manifest_path = evidence_root / manifest_relative
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        health = manifest.get("capture_health") or {}
        if health.get("collector_started_before_first_mutation") is not True:
            raise AssertionError(f"Collector did not start before navigation: {health}")
        if health.get("collector_stopped") is not True:
            raise AssertionError(f"Collector did not stop: {health}")
        if health.get("orphan_capture_id") is not None:
            raise AssertionError(f"Orphan capture remained: {health}")
        waits = manifest.get("wait_observations") or []
        matched_ids = [
            str(item["matched_request_ids"][0])
            for item in waits
            if isinstance(item, dict) and item.get("matched_request_ids")
        ]
        if len(matched_ids) < 2 or len(set(matched_ids[-2:])) != 2:
            raise AssertionError(
                f"Sequential waits did not bind to two requests: {waits}"
            )
        raw = artifact_by_kind(manifest, "raw_bytes")
        events = artifact_by_kind(manifest, "events", "eventsource_events")
        headers = artifact_by_kind(
            manifest,
            "request_headers_redacted",
        )
        raw_relative = relative_path(raw)
        events_relative = relative_path(events)
        headers_relative = relative_path(headers)
        workspace = AnalysisWorkspaceService(evidence_root)
        inspected = await workspace.inspect(
            WorkspaceInspectRequest(
                paths=[f"experiments/{captured.experiment_id}"],
                queries=["[DONE]"],
                max_depth=8,
                max_tree_entries=500,
                max_read_files=5,
                max_file_lines=200,
            )
        )
        tree_paths = {item.path for item in inspected.tree}
        for required in [manifest_relative, events_relative, headers_relative, raw_relative]:
            if required not in tree_paths:
                raise AssertionError(f"workspaceInspect missed {required}")
        read = await workspace.read_files(
            WorkspaceReadFilesRequest(
                paths=[manifest_relative, events_relative, headers_relative],
                max_lines=5_000,
                max_bytes_per_file=1_000_000,
                max_bytes=3_000_000,
            )
        )
        if any(item.error for item in read.files):
            raise AssertionError(f"workspaceReadFiles errors: {read.model_dump()}")
        events_text = next(
            item.content for item in read.files if item.path == events_relative
        )
        if "[DONE]" not in events_text:
            raise AssertionError("events artifact did not contain [DONE]")
        quoted_raw = raw_relative.replace("'", "''")
        binary = await workspace.exec_pwsh(
            WorkspaceExecPwshRequest(
                script=(
                    f"$bytes = [IO.File]::ReadAllBytes('{quoted_raw}')\n"
                    "$hash = [Security.Cryptography.SHA256]::HashData($bytes)\n"
                    "$sha = [Convert]::ToHexString($hash).ToLowerInvariant()\n"
                    "Write-Output ('bytes=' + $bytes.Length)\n"
                    "Write-Output ('sha256=' + $sha)\n"
                    "$headLength = [Math]::Min(32, $bytes.Length)\n"
                    "if ($headLength -gt 0) {\n"
                    "  $head = [Convert]::ToBase64String($bytes[0..($headLength - 1)])\n"
                    "  Write-Output ('headBase64=' + $head)\n"
                    "}"
                ),
                timeout_seconds=15,
                max_output_bytes=10_000,
                plain_output=True,
                utf8_output=True,
            )
        )
        expected_raw = b"".join(SSE_EVENTS)
        expected_sha = hashlib.sha256(expected_raw).hexdigest()
        if f"bytes={len(expected_raw)}" not in binary.stdout:
            raise AssertionError(binary.stdout)
        if f"sha256={expected_sha}" not in binary.stdout:
            raise AssertionError(binary.stdout)

        pandora_capture = await service.run(
            CaptureFlowRequest(
                operation="capture_flow",
                payload={
                    "session_id": SESSION_ID,
                    "objective": "capture one authenticated Pandora-like request",
                    "primary_request": {
                        "url_contains": "/api/pandora/conversation",
                        "method": "POST",
                        "resource_types": ["fetch"],
                        "mime_types": ["text/event-stream"],
                        "expected_min_matches": 1,
                        "expected_max_matches": 1,
                        "allow_supporting_failures": True,
                        "include_in_flight": False,
                    },
                    "flow": [
                        {
                            "step_id": "send_pandora_request",
                            "action": "click",
                            "locator": {"css": "#send-pandora"},
                            "timeout_ms": 10_000,
                        },
                        {
                            "step_id": "wait_pandora_200",
                            "action": "wait",
                            "condition": {
                                "type": "selector_visible",
                                "locator": {"text": "pandora-200"},
                                "timeout_ms": 10_000,
                            },
                        },
                    ],
                    "execution_mode": "sync",
                    "deadline_ms": 30_000,
                    "capture": {
                        "network": True,
                        "stream": True,
                        "trace": False,
                        "screenshots": False,
                        "page_snapshots": True,
                        "console_errors": True,
                    },
                    "requirements": {
                        "require_raw_capture": True,
                        "require_semantic_parse": False,
                        "require_request_snapshot": True,
                        "require_artifacts": True,
                    },
                    "network_evidence": [
                        {
                            "selector_id": "pandora_conversation",
                            "matcher": {
                                "url_contains": "/api/pandora/conversation",
                                "method": "POST",
                                "resource_types": ["fetch"],
                            },
                            "max_matches": 1,
                            "export_parts": ["all"],
                            "include_initiator": True,
                        }
                    ],
                    "series": {
                        "analysis_series_id": "pandora-fixture-series",
                        "scenario_type": "first_message",
                        "sequence_index": 1,
                        "conversation_key": "conversation-fixture",
                    },
                },
            )
        )
        if pandora_capture.status != "completed":
            raise AssertionError(pandora_capture.model_dump())
        pandora_manifest_path = (
            evidence_root
            / "experiments"
            / str(pandora_capture.experiment_id)
            / "manifest.json"
        )
        pandora_manifest = json.loads(
            pandora_manifest_path.read_text(encoding="utf-8")
        )
        source_evidence = next(
            item
            for item in pandora_manifest.get("evidence", [])
            if item.get("kind") == "network_request"
            and item.get("selector_id") == "pandora_conversation"
        )
        if source_evidence.get("request_ids", {}).get("reqid") in {
            None,
            0,
        }:
            raise AssertionError(source_evidence)
        public_evidence = json.dumps(source_evidence, ensure_ascii=False)
        if "fixture-token" in public_evidence or "fixture-session" in public_evidence:
            raise AssertionError("Public evidence leaked fixture credentials")

        shape = await service.inspect(
            GetRequestShapeRequest(
                operation="get_request_shape",
                payload={
                    "experiment_id": pandora_capture.experiment_id,
                    "evidence_id": source_evidence["evidence_id"],
                },
            )
        )
        shape_paths = (
            shape.result.get("request_shape", {}).get("paths", {})
            if isinstance(shape.result, dict)
            else {}
        )
        for expected_pointer in [
            "/messages/0/id",
            "/messages/0/content/parts/0",
            "/parent_message_id",
            "/tracking_id",
        ]:
            if expected_pointer not in shape_paths:
                raise AssertionError(shape.model_dump())

        control = await service.run(
            ReplayRequestRequest(
                operation="replay_request",
                payload={
                    "session_id": SESSION_ID,
                    "objective": "Pandora-like control replay",
                    "source_experiment_id": pandora_capture.experiment_id,
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
                    "deadline_ms": 30_000,
                    "capture": {
                        "network": True,
                        "stream": False,
                        "trace": False,
                        "screenshots": False,
                        "page_snapshots": True,
                        "console_errors": True,
                    },
                    "series": {
                        "analysis_series_id": "pandora-fixture-series",
                        "scenario_type": "control_replay",
                        "predecessor_experiment_id": pandora_capture.experiment_id,
                        "sequence_index": 2,
                        "conversation_key": "conversation-fixture",
                    },
                },
            )
        )
        if control.status != "completed":
            raise AssertionError(control.model_dump())
        control_manifest = json.loads(
            (
                evidence_root
                / "experiments"
                / str(control.experiment_id)
                / "manifest.json"
            ).read_text(encoding="utf-8")
        )
        if control_manifest.get("replay_http_status") != 200:
            raise AssertionError(control_manifest)
        if not control_manifest.get("replay", {}).get("source_is_stream"):
            raise AssertionError(control_manifest)
        control_stream = next(
            (
                item
                for item in control_manifest.get("evidence", [])
                if item.get("kind") == "stream_request"
            ),
            None,
        )
        if not control_stream:
            raise AssertionError("Control replay produced no stream_request evidence")

        async def run_replay(
            *,
            scenario_type: str,
            sequence_index: int,
            mutation: dict[str, Any],
        ) -> tuple[dict[str, Any], int]:
            replay = await service.run(
                ReplayRequestRequest(
                    operation="replay_request",
                    payload={
                        "session_id": SESSION_ID,
                        "objective": f"Pandora-like replay: {scenario_type}",
                        "source_experiment_id": pandora_capture.experiment_id,
                        "source_evidence_id": source_evidence["evidence_id"],
                        "replay_mode": "treatment",
                        "control_experiment_id": control.experiment_id,
                        "mutations": [mutation],
                        "execution_mode": "sync",
                        "deadline_ms": 30_000,
                        "capture": {
                            "network": True,
                            "stream": False,
                            "trace": False,
                            "screenshots": False,
                            "page_snapshots": True,
                            "console_errors": True,
                        },
                        "series": {
                            "analysis_series_id": "pandora-fixture-series",
                            "scenario_type": scenario_type,
                            "predecessor_experiment_id": control.experiment_id,
                            "sequence_index": sequence_index,
                            "conversation_key": "conversation-fixture",
                        },
                    },
                )
            )
            if replay.status != "completed":
                raise AssertionError(replay.model_dump())
            replay_manifest = json.loads(
                (
                    evidence_root
                    / "experiments"
                    / str(replay.experiment_id)
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            response_artifact = artifact_by_kind(
                replay_manifest,
                "replay_response",
            )
            response_value = json.loads(
                (evidence_root / relative_path(response_artifact)).read_text(
                    encoding="utf-8"
                )
            )
            response_status = find_numeric_status(response_value)
            if response_status is None:
                raise AssertionError(response_value)
            return replay_manifest, response_status

        tracking_manifest, tracking_status = await run_replay(
            scenario_type="remove_tracking_field",
            sequence_index=3,
            mutation={
                "type": "remove_json_path",
                "path": "/tracking_id",
            },
        )
        if tracking_status != 200:
            raise AssertionError(tracking_manifest)
        required_manifest, required_status = await run_replay(
            scenario_type="remove_required_message_id",
            sequence_index=4,
            mutation={
                "type": "remove_json_path",
                "path": "/messages/0/id",
            },
        )
        if required_status != 422:
            raise AssertionError(required_manifest)
        for replay_manifest in [tracking_manifest, required_manifest]:
            replay_attempt = next(
                item
                for item in replay_manifest.get("evidence", [])
                if item.get("kind") == "replay_attempt"
            )
            if replay_attempt.get("source_evidence_id") != source_evidence.get(
                "evidence_id"
            ):
                raise AssertionError(replay_attempt)
            if replay_attempt.get("control_experiment_id") != control.experiment_id:
                raise AssertionError(replay_attempt)
            mutation_assessment = replay_manifest.get("mutation_assessment") or {}
            if mutation_assessment.get("mutation_effective") is not True:
                raise AssertionError(mutation_assessment)
            diff_artifact = artifact_by_kind(
                replay_manifest,
                "replay_request_diff",
            )
            diff_text = (
                evidence_root / relative_path(diff_artifact)
            ).read_text(encoding="utf-8")
            if "fixture-token" in diff_text or "fixture-session" in diff_text:
                raise AssertionError("Replay diff leaked fixture credentials")
        tracking_stream = next(
            (
                item
                for item in tracking_manifest.get("evidence", [])
                if item.get("kind") == "stream_request"
            ),
            None,
        )
        if not tracking_stream:
            raise AssertionError("Tracking treatment produced no stream evidence")
        if not required_manifest.get("protocol_rejection_observed"):
            raise AssertionError(required_manifest)

        cancellation_started = await service.run(
            CaptureFlowRequest(
                operation="capture_flow",
                payload={
                    "session_id": SESSION_ID,
                    "objective": "cancel a real slow navigation",
                    "primary_request": {
                        "url_contains": "/api/sse",
                        "method": "GET",
                        "resource_types": ["eventsource"],
                        "mime_types": ["text/event-stream"],
                        "expected_min_matches": 0,
                        "allow_supporting_failures": True,
                        "include_in_flight": False,
                    },
                    "flow": [
                        {
                            "step_id": "cancel_slow_navigation",
                            "action": "navigate",
                            "value": f"http://127.0.0.1:{fixture_port}/slow?seconds=10",
                            "timeout_ms": 20_000,
                        }
                    ],
                    "execution_mode": "job",
                    "job_timeout_ms": 30_000,
                    "capture": {
                        "network": False,
                        "stream": True,
                        "trace": True,
                        "screenshots": False,
                        "page_snapshots": False,
                        "console_errors": False,
                    },
                },
            )
        )
        if cancellation_started.status != "running":
            raise AssertionError(cancellation_started.model_dump())
        slow_started = server.RequestHandlerClass.slow_started_event
        for _ in range(200):
            if slow_started.is_set():
                break
            await asyncio.sleep(0.05)
        if not slow_started.is_set():
            raise AssertionError("Slow navigation was not observed by the fixture server")
        canceled = await service.run(
            CancelExperimentRequest(
                operation="cancel_experiment",
                payload={
                    "experiment_id": cancellation_started.experiment_id,
                    "session_id": SESSION_ID,
                },
            )
        )
        if canceled.status != "interrupted":
            raise AssertionError(canceled.model_dump())
        cancellation_manifest_path = (
            evidence_root
            / "experiments"
            / str(cancellation_started.experiment_id)
            / "manifest.json"
        )
        cancellation_manifest = json.loads(
            cancellation_manifest_path.read_text(encoding="utf-8")
        )
        if cancellation_manifest.get("status") != "interrupted":
            raise AssertionError(cancellation_manifest)
        cancellation_steps = cancellation_manifest.get("steps") or []
        if not cancellation_steps or cancellation_steps[0].get("status") != (
            "canceled_outcome_unknown"
        ):
            raise AssertionError(cancellation_steps)
        cancellation_health = cancellation_manifest.get("capture_health") or {}
        if cancellation_health.get("collector_cleanup") != "completed":
            raise AssertionError(cancellation_health)
        if cancellation_health.get("orphan_capture_id") is not None:
            raise AssertionError(cancellation_health)

        closed_response = await service.run(
            CloseSessionRequest(
                operation="close_session",
                payload={"session_id": SESSION_ID},
            )
        )
        if closed_response.status != "completed":
            raise AssertionError(f"close_session failed: {closed_response.model_dump()}")
        closed = True
        await service.close()
        service = None
        deadline = time.monotonic() + 5
        residual: list[str] = []
        while time.monotonic() < deadline:
            residual = process_matches(
                [SESSION_ID, str(js_reverse_entry)],
                os.getpid(),
            )
            if not residual:
                break
            await asyncio.sleep(0.1)
        if residual:
            raise AssertionError(f"Residual browser helper processes: {residual}")
        return {
            "status": "passed",
            "experiment_id": captured.experiment_id,
            "manifest_relative_path": manifest_relative,
            "objective_integrity": manifest.get("objective_integrity"),
            "raw_bytes": len(expected_raw),
            "raw_sha256": expected_sha,
            "trace_count": len(manifest.get("trace_paths") or []),
            "screenshot_count": len(manifest.get("screenshot_paths") or []),
            "collector_started_before_first_mutation": health.get(
                "collector_started_before_first_mutation"
            ),
            "collector_stopped": health.get("collector_stopped"),
            "sequential_request_ids": matched_ids[-2:],
            "pandora_source_experiment_id": pandora_capture.experiment_id,
            "pandora_source_evidence_id": source_evidence["evidence_id"],
            "pandora_control_experiment_id": control.experiment_id,
            "pandora_control_status": control_manifest.get("replay_http_status"),
            "pandora_tracking_replay_status": tracking_status,
            "pandora_tracking_mutation_effective": tracking_manifest.get(
                "mutation_assessment", {}
            ).get("mutation_effective"),
            "pandora_required_replay_status": required_status,
            "pandora_required_mutation_effective": required_manifest.get(
                "mutation_assessment", {}
            ).get("mutation_effective"),
            "pandora_required_protocol_rejection": required_manifest.get(
                "protocol_rejection_observed"
            ),
            "cancellation_status": cancellation_manifest.get("status"),
            "cancellation_step_status": cancellation_steps[0].get("status"),
            "cancellation_collector_cleanup": cancellation_health.get(
                "collector_cleanup"
            ),
            "residual_processes": [],
        }
    finally:
        if service is not None:
            if not closed:
                try:
                    await service.run(
                        CloseSessionRequest(
                            operation="close_session",
                            payload={"session_id": SESSION_ID},
                        )
                    )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(service.close(), timeout=10)
            except Exception:
                pass
        if chrome_process is not None:
            stop_process(chrome_process)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        shutil.rmtree(chrome_profile, ignore_errors=True)
        shutil.rmtree(evidence_root, ignore_errors=True)
        shutil.rmtree(repo_root / ".playwright", ignore_errors=True)
        shutil.rmtree(repo_root / ".playwright-cli", ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real Windows BrowserAction end-to-end smoke test."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument(
        "--js-reverse-entry",
        type=Path,
        required=True,
        help="Built js-reverse-mcp entrypoint, for example build/src/main.js.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = asyncio.run(
        run_smoke(
            args.repo_root.resolve(),
            args.js_reverse_entry.resolve(),
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
