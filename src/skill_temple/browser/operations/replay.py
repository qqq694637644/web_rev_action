"""Replay planning, source resolution, extraction, and effective protocol state."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ...browser_models import (
    CaptureFlowPayload,
    FlowStepResult,
    PrimaryRequest,
    ReplayBinding,
    ReplayRequestPayload,
    ReplayRequestRequest,
    RequestMatcher,
)
from ...protocol.fingerprints import (
    canonical_json_sha256,
    request_body_canonical_sha256_from_spec,
)
from ...protocol.matching import (
    network_checkpoint,
    network_request_matches,
    requests_after_checkpoint,
)
from ...protocol.mutations import (
    binding_value_from_snapshot,
    build_replay_spec,
    json_pointer_value,
)
from ...protocol_evidence import (
    load_snapshot,
    response_content_type,
    response_value_from_snapshot,
)
from ..adapters.contracts import AlignmentResult, StreamCheckpoint
from ..core import BrowserServiceError, Deadline, utc_now
from ..steps import StepExecutor
from .context import ReplayDispatchResult, ReplayPreparationResult


class BrowserReplayOperations:
    """Own replay behavior while the public service remains a facade."""

    def _write_stream_runtime(
        self,
        *,
        experiment_id: str,
        manifest: dict[str, Any],
        start_status: str,
        capture_id: int | None,
        capture_uuid: str | None,
        capture_relative_dir: str | None,
        capture_metadata_artifact_id: str | None,
        transport_generation: int | None,
    ) -> None:
        manifest["stream_runtime"] = {
            "start_status": start_status,
            "capture_id": capture_id,
            "capture_uuid": capture_uuid,
            "capture_relative_dir": capture_relative_dir,
            "capture_metadata_artifact_id": capture_metadata_artifact_id,
            "transport_generation": transport_generation,
            "capture_namespace": experiment_id,
        }
        self.experiments.write_manifest(experiment_id, manifest)

    @staticmethod
    def _pointer_tokens(path: str) -> list[str]:
        if path == "/":
            return []
        return [token.replace("~1", "/").replace("~0", "~") for token in path.split("/")[1:]]

    @staticmethod
    def _generate_binding_value(binding: ReplayBinding) -> Any:
        if binding.value_source != "generated" or binding.generator is None:
            raise ValueError("Only generated bindings can generate a new value")
        if binding.generator == "uuid4":
            return str(uuid.uuid4())
        if binding.generator == "timestamp_ms":
            return int(time.time() * 1000)
        if binding.generator == "timestamp_iso":
            return datetime.now(UTC).isoformat()
        return secrets.token_hex(16)

    @classmethod
    def _initial_replay_binding_values(
        cls,
        bindings: list[ReplayBinding],
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for binding in bindings:
            if binding.value_source == "generated":
                values[binding.binding_id] = cls._generate_binding_value(binding)
            elif binding.value_source == "preserve_source":
                values[binding.binding_id] = binding_value_from_snapshot(snapshot, binding)
            elif binding.value_source in {"literal", "manual_input"}:
                values[binding.binding_id] = binding.value
        return values

    @staticmethod
    def _public_binding_specs(bindings: list[ReplayBinding]) -> list[dict[str, Any]]:
        values = [item.model_dump(mode="json", exclude_none=True) for item in bindings]
        for binding in values:
            if "value" in binding:
                binding["value_sha256"] = canonical_json_sha256(binding.pop("value"))
        return values

    @classmethod
    def _replay_protocol(cls, payload: ReplayRequestPayload) -> dict[str, Any]:
        protocol = payload.model_dump(mode="json", exclude_none=True, exclude={"objective"})
        protocol["bindings"] = cls._public_binding_specs(list(payload.bindings))
        return protocol

    @staticmethod
    def _apply_observed_replay_mode(
        replay_plan: dict[str, Any],
        observed_mode: str | None,
    ) -> None:
        valid_modes = {"ordinary", "sse", "ndjson", "raw_stream"}
        if observed_mode not in valid_modes:
            return
        protocol = json.loads(json.dumps(replay_plan["replay_protocol"]))
        response_reader = protocol.get("response_reader")
        response_reader = response_reader if isinstance(response_reader, dict) else {}
        requested_mode = str(response_reader.get("mode") or "auto")
        if requested_mode == "auto":
            response_reader["mode"] = observed_mode
        protocol["response_reader"] = response_reader
        response_is_stream = observed_mode in {"sse", "ndjson", "raw_stream"}
        protocol["observed_response_mode"] = observed_mode
        protocol["response_is_stream"] = response_is_stream
        if response_is_stream:
            capture = protocol.get("capture")
            capture = capture if isinstance(capture, dict) else {}
            capture["stream"] = True
            protocol["capture"] = capture
            requirements = protocol.get("requirements")
            requirements = requirements if isinstance(requirements, dict) else {}
            requirements["require_raw_capture"] = True
            requirements["require_semantic_parse"] = not bool(
                response_reader.get("raw_only")
            )
            requirements["require_artifacts"] = True
            protocol["requirements"] = requirements
        replay_plan["replay_protocol"] = protocol
        replay_plan["replay_protocol_hash"] = canonical_json_sha256(protocol)
        replay_plan["observed_response_mode"] = observed_mode
        replay_plan["response_is_stream"] = response_is_stream

    @staticmethod
    def _binding_observations(
        bindings: list[ReplayBinding],
        values: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [
            {
                "binding_id": binding.binding_id,
                "target": binding.target,
                "value_source": binding.value_source,
                "extractor_id": binding.extractor_id,
                "resolved": binding.binding_id in values,
                **(
                    {"value_sha256": canonical_json_sha256(values[binding.binding_id])}
                    if binding.binding_id in values
                    else {}
                ),
            }
            for binding in bindings
        ]

    async def _all_network_requests(self, deadline: Deadline) -> list[dict[str, Any]]:
        payload = await self.js_reverse.list_network_requests(
            RequestMatcher(),
            deadline,
        )
        requests = payload.get("requests")
        return (
            [item for item in requests if isinstance(item, dict)]
            if isinstance(requests, list)
            else []
        )

    async def _run_replay_extractors(
        self,
        *,
        extractors: list[Any],
        checkpoint: dict[str, Any],
        experiment_dir: Path,
        deadline: Deadline,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        requests = requests_after_checkpoint(
            await self._all_network_requests(deadline),
            checkpoint,
            include_in_flight=True,
        )
        values: dict[str, Any] = {}
        records: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        output_dir = experiment_dir / "replay" / "extractors"
        output_dir.mkdir(parents=True, exist_ok=True)
        for extractor in extractors:
            artifact_ids: list[str] = []
            record: dict[str, Any] = {
                "extractor_id": extractor.extractor_id,
                "type": extractor.type,
                "required": extractor.required,
                "status": "failed",
                "artifact_ids": artifact_ids,
            }
            try:
                matches = [
                    item for item in requests if network_request_matches(item, extractor.selector)
                ]
                occurrence = extractor.occurrence
                if occurrence == "first":
                    selected = matches[0] if matches else None
                elif occurrence == "last":
                    selected = matches[-1] if matches else None
                else:
                    selected = matches[occurrence] if occurrence < len(matches) else None
                if not isinstance(selected, dict) or not isinstance(selected.get("reqid"), int):
                    raise ValueError("no matching network response was observed")
                reqid = int(selected["reqid"])
                snapshot_path = output_dir / f"{extractor.extractor_id}.json"
                await self.js_reverse.export_network_request(
                    reqid,
                    snapshot_path,
                    "all",
                    deadline.child(min(5_000, deadline.remaining_ms())),
                )
                artifact_id_value = (
                    f"art_{experiment_dir.name}_replay_extractor_{extractor.extractor_id}"
                )
                descriptor = self.experiments.describe_local_artifact(
                    str(snapshot_path),
                    artifact_id=artifact_id_value,
                    kind="replay_extractor_snapshot",
                    sensitivity="credential",
                    contains_credentials=True,
                )
                if descriptor is not None:
                    artifacts.append(descriptor)
                    artifact_ids.append(artifact_id_value)
                snapshot = load_snapshot(snapshot_path)
                response_value = response_value_from_snapshot(snapshot)
                if response_value is None:
                    raise ValueError("the selected response body is unavailable")
                value = json_pointer_value(response_value, extractor.pointer)
                values[extractor.extractor_id] = value
                record.update(
                    {
                        "status": "completed",
                        "pointer": extractor.pointer,
                        "request_id": reqid,
                        "request_url_sha256": canonical_json_sha256(str(selected.get("url") or "")),
                        "value_sha256": canonical_json_sha256(value),
                    }
                )
            except Exception as exc:
                record["error"] = str(exc)[:2000]
            records.append(record)
        return values, records, artifacts

    @classmethod
    def _extract_response_field(cls, value: Any, field: str) -> Any:
        if isinstance(value, dict):
            if field in value:
                return value[field]
            for child in value.values():
                found = cls._extract_response_field(child, field)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._extract_response_field(child, field)
                if found is not None:
                    return found
        elif isinstance(value, str):
            try:
                return cls._extract_response_field(json.loads(value), field)
            except json.JSONDecodeError:
                return None
        return None

    @classmethod
    def _complete_replay_response_value(cls, value: Any) -> Any | None:
        preview = cls._extract_response_field(value, "bodyPreview")
        byte_length = cls._extract_response_field(value, "bodyByteLength")
        truncated = bool(cls._extract_response_field(value, "truncated"))
        termination = cls._extract_response_field(value, "terminationReason")
        if (
            not isinstance(preview, str)
            or not isinstance(byte_length, int)
            or truncated
            or termination not in {"network_close", "no_response_body"}
            or len(preview.encode("utf-8")) != byte_length
        ):
            return None
        try:
            return json.loads(preview)
        except json.JSONDecodeError:
            return preview

    def _prepare_replay_execution(
        self,
        request: ReplayRequestRequest,
    ) -> tuple[CaptureFlowPayload, dict[str, Any]]:
        payload = request.payload
        mutations = list(payload.mutations)
        binding_specs = list(payload.bindings)
        requested_replay_protocol = self._replay_protocol(payload)
        requested_replay_protocol_hash = canonical_json_sha256(
            requested_replay_protocol
        )
        source_manifest = self.experiments.load_manifest(payload.source.experiment_id)
        if source_manifest.get("session_id") != payload.session_id:
            raise BrowserServiceError(
                "source_experiment_session_mismatch",
                "Replay source evidence belongs to a different browser session.",
                409,
            )
        source_evidence = self._find_evidence(
            source_manifest,
            payload.source.evidence_id,
        )
        if source_evidence.get("kind") != "network_request":
            raise BrowserServiceError(
                "replay_source_kind_invalid",
                "replay_request requires network_request evidence.",
                409,
            )
        artifact_paths = source_evidence.get("artifact_paths")
        artifact_paths = artifact_paths if isinstance(artifact_paths, dict) else {}
        snapshot_relative = artifact_paths.get("all")
        if not isinstance(snapshot_relative, str):
            raise BrowserServiceError(
                "replay_source_snapshot_missing",
                "The source evidence has no exact full network snapshot artifact.",
                409,
            )
        snapshot_path = (self.experiments.root / snapshot_relative).resolve()
        try:
            snapshot_path.relative_to(self.experiments.root)
        except ValueError as exc:
            raise BrowserServiceError(
                "replay_source_path_invalid",
                "The source snapshot is outside the analysis workspace.",
                409,
            ) from exc
        if not snapshot_path.is_file():
            raise BrowserServiceError(
                "replay_source_snapshot_missing",
                "The source network snapshot file is missing.",
                404,
            )
        try:
            snapshot = load_snapshot(snapshot_path)
            binding_values = self._initial_replay_binding_values(binding_specs, snapshot)
            resolved_bindings = [
                item for item in binding_specs if item.binding_id in binding_values
            ]
            spec, diff = build_replay_spec(
                snapshot,
                mutations,
                bindings=resolved_bindings,
                binding_values=binding_values,
                query_serialization=payload.query_serialization,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise BrowserServiceError(
                "replay_source_invalid",
                str(exc),
                409,
            ) from exc
        source_url = str(snapshot.get("url", ""))
        source_method = str(snapshot.get("method", "GET")).upper()
        source_resource_type = str(snapshot.get("resourceType", "fetch"))
        source_content_type = response_content_type(snapshot)
        reader = payload.response_reader
        termination = payload.termination
        source_is_stream = reader.mode in {
            "sse",
            "ndjson",
            "raw_stream",
        } or (
            reader.mode == "auto"
            and source_content_type
            in {"text/event-stream", "application/x-ndjson", "application/ndjson"}
        )
        stream_capture_enabled = source_is_stream or reader.mode == "auto"
        matcher = RequestMatcher(
            url_contains=source_url.split("?", 1)[0],
            method=source_method,
            resource_types=[source_resource_type] if source_resource_type else [],
        )
        primary = PrimaryRequest(
            url_contains=matcher.url_contains,
            method=matcher.method,
            resource_types=matcher.resource_types,
            mime_types=[source_content_type] if source_content_type else [],
            expected_min_matches=1,
            expected_max_matches=5,
            allow_supporting_failures=True,
            include_in_flight=False,
        )
        selectors: list[Any] = [
            {
                "selector_id": "replay_request",
                "matcher": matcher.model_dump(mode="json", exclude_none=True),
                "max_matches": 20,
                "export_parts": ["all"],
                "include_initiator": True,
            },
            *payload.network_evidence,
        ]
        capture = payload.capture.model_dump(mode="json")
        requirements = payload.requirements.model_dump(mode="json")
        wait_for = payload.wait_for
        if stream_capture_enabled:
            capture["stream"] = True
        if source_is_stream:
            requirements["require_raw_capture"] = True
            requirements["require_semantic_parse"] = not reader.raw_only
            requirements["require_artifacts"] = True
        terminal_conditions = [
            item.model_dump(mode="json", exclude_none=True)
            for item in termination.conditions
        ]
        exact_sse_condition = next(
            (
                item
                for item in terminal_conditions
                if item.get("type") == "exact_sse_data"
            ),
            None,
        )
        idle_window_condition = next(
            (
                item
                for item in terminal_conditions
                if item.get("type") == "idle_window"
            ),
            None,
        )
        spec["responseControl"] = {
            "maxResponseBytes": reader.max_bytes,
            "maxEvents": reader.max_events,
            "idleWindowMs": (
                idle_window_condition.get("window_ms")
                if isinstance(idle_window_condition, dict)
                else None
            ),
            "responseMode": reader.mode,
            "terminalConditions": terminal_conditions,
            "doneMarker": (
                exact_sse_condition.get("value")
                if isinstance(exact_sse_condition, dict)
                else None
            ),
            "doneEventName": (
                exact_sse_condition.get("event_name")
                if isinstance(exact_sse_condition, dict)
                else None
            ),
        }
        spec["transport"] = payload.transport.model_dump(mode="json")
        series = payload.series.model_dump(mode="json", exclude_none=True)
        series["scenario_type"] = series.get("scenario_type") or "generic_replay"
        normalized = CaptureFlowPayload.model_validate(
            {
                "session_id": payload.session_id,
                "objective": payload.objective,
                "target": payload.target.model_dump(mode="json", exclude_none=True),
                "primary_request": primary.model_dump(mode="json"),
                "flow": [
                    item.model_dump(mode="json", exclude_none=True)
                    for item in payload.verification_flow
                ],
                "wait_for": (
                    wait_for.model_dump(mode="json", exclude_none=True) if wait_for else None
                ),
                "execution_mode": payload.execution_mode,
                "deadline_ms": payload.deadline_ms,
                "job_timeout_ms": payload.job_timeout_ms,
                "capture": capture,
                "requirements": requirements,
                "network_evidence": [
                    item.model_dump(mode="json", exclude_none=True)
                    if hasattr(item, "model_dump")
                    else item
                    for item in selectors
                ],
                "series": series,
            }
        )
        replay_protocol = json.loads(json.dumps(requested_replay_protocol))
        replay_protocol["capture"] = normalized.capture.model_dump(mode="json")
        replay_protocol["requirements"] = normalized.requirements.model_dump(
            mode="json"
        )
        replay_protocol["network_evidence"] = [
            item.model_dump(mode="json", exclude_none=True)
            for item in normalized.network_evidence
        ]
        replay_protocol["source_is_stream"] = source_is_stream
        replay_protocol["stream_capture_enabled"] = stream_capture_enabled
        replay_protocol_hash = canonical_json_sha256(replay_protocol)
        replay_attempt_id = f"replay_{uuid.uuid4().hex}"
        comparison = (
            payload.comparison.model_dump(mode="json", exclude_none=True)
            if payload.comparison
            else None
        )
        environment = (
            comparison.get("environment")
            if isinstance(comparison, dict) and "environment" in comparison.get("dimensions", [])
            else None
        )
        return normalized, {
            "source_experiment_id": payload.source.experiment_id,
            "source_evidence_id": payload.source.evidence_id,
            "source_snapshot_path": snapshot_path,
            "source_evidence": source_evidence,
            "source_content_type": source_content_type,
            "source_is_stream": source_is_stream,
            "stream_capture_enabled": stream_capture_enabled,
            "bindings": self._public_binding_specs(binding_specs),
            "binding_values": binding_values,
            "binding_observations": self._binding_observations(
                binding_specs,
                binding_values,
            ),
            "unresolved_binding_ids": sorted(
                item.binding_id for item in binding_specs if item.binding_id not in binding_values
            ),
            "replay_protocol": replay_protocol,
            "replay_protocol_hash": replay_protocol_hash,
            "requested_replay_protocol": requested_replay_protocol,
            "requested_replay_protocol_hash": requested_replay_protocol_hash,
            "setup_flow": [
                step.model_dump(mode="json", exclude_none=True) for step in payload.setup_flow
            ],
            "extractors": [
                item.model_dump(mode="json", exclude_none=True) for item in payload.extractors
            ],
            "_setup_flow_steps": list(payload.setup_flow),
            "_extractors": list(payload.extractors),
            "_source_snapshot": snapshot,
            "_binding_specs": binding_specs,
            "query_serialization": payload.query_serialization,
            "comparison": comparison,
            "environment_comparison": environment,
            "ignored_cookie_names": list(environment.get("ignored_cookie_names", []))
            if isinstance(environment, dict)
            else [],
            "ignored_context_headers": list(environment.get("ignored_context_headers", []))
            if isinstance(environment, dict)
            else [],
            "response_analyzer": (
                reader.analyzer.model_dump(mode="json") if reader.analyzer else None
            ),
            "transport": payload.transport.model_dump(mode="json"),
            "mutations": mutations,
            "replay_attempt_id": replay_attempt_id,
            "expected_request_body_canonical_sha256": (
                request_body_canonical_sha256_from_spec(spec)
            ),
            "spec": spec,
            "diff": diff,
        }

    @staticmethod
    def _replay_manifest_seed(replay_plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_experiment_id": replay_plan["source_experiment_id"],
            "source_evidence_id": replay_plan["source_evidence_id"],
            "source_content_type": replay_plan["source_content_type"],
            "source_is_stream": replay_plan["source_is_stream"],
            "stream_capture_enabled": replay_plan["stream_capture_enabled"],
            "bindings": replay_plan["bindings"],
            "binding_observations": replay_plan["binding_observations"],
            "unresolved_binding_ids": replay_plan["unresolved_binding_ids"],
            "extractors": replay_plan["extractors"],
            "comparison": replay_plan["comparison"],
            "replay_protocol": replay_plan["replay_protocol"],
            "replay_protocol_hash": replay_plan["replay_protocol_hash"],
            "requested_replay_protocol": replay_plan[
                "requested_replay_protocol"
            ],
            "requested_replay_protocol_hash": replay_plan[
                "requested_replay_protocol_hash"
            ],
            "replay_attempt_id": replay_plan["replay_attempt_id"],
            "expected_request_body_canonical_sha256": replay_plan[
                "expected_request_body_canonical_sha256"
            ],
        }
    async def _execute_replay_dispatch(
        self,
        *,
        experiment_id: str,
        experiment_dir: Path,
        manifest: dict[str, Any],
        replay_plan: dict[str, Any],
        session_id: str,
        session: dict[str, Any],
        deadline: Deadline,
        capture_id: int | None,
        request_matcher: RequestMatcher,
        stream_checkpoint: StreamCheckpoint,
        first_mutation_wall_time_ms: int | None,
        step_results: list[FlowStepResult],
        warnings: list[str],
    ) -> ReplayDispatchResult:
        replay_result: dict[str, Any] = {}
        replay_response: Any = None
        replay_http_status: int | None = None
        replay_response_content_type: str | None = None
        replay_observed_response_mode: str | None = None
        post_response_alignment: AlignmentResult | None = None
        replay_artifacts: list[dict[str, Any]] = []
        if capture_id is not None:
            stream_checkpoint = await self._stream_checkpoint(
                capture_id,
                request_matcher,
                self._operation_deadline(
                    deadline,
                    1_500,
                    "checkpoint before replay",
                ),
            )
        first_mutation_wall_time_ms = int(time.time() * 1000)
        replay_dir = experiment_dir / "replay"
        replay_dir.mkdir(parents=True, exist_ok=True)
        spec_file = replay_dir / "request-spec.json"
        diff_file = replay_dir / "request-diff.json"
        result_file = replay_dir / "response.json"
        spec_file.write_text(
            json.dumps(replay_plan["spec"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        diff_file.write_text(
            json.dumps(replay_plan["diff"], ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        started = utc_now()
        try:
            replay_plan["dispatch_wall_time_ms"] = int(time.time() * 1000)
            replay_plan["correlation_window_end_wall_time_ms"] = replay_plan[
                "dispatch_wall_time_ms"
            ] + max(1_000, deadline.remaining_ms())
            replay_manifest = manifest.get("replay")
            if isinstance(replay_manifest, dict):
                replay_manifest["dispatch_wall_time_ms"] = replay_plan[
                    "dispatch_wall_time_ms"
                ]
                replay_manifest["correlation_window_end_wall_time_ms"] = replay_plan[
                    "correlation_window_end_wall_time_ms"
                ]
                self.experiments.write_manifest(experiment_id, manifest)
            replay_result = await self.js_reverse.evaluate_browser_replay(
                spec_file,
                result_file,
                self._operation_deadline(
                    deadline,
                    20_000,
                    "browser-context replay",
                ),
            )
            if result_file.is_file():
                try:
                    replay_response = json.loads(
                        result_file.read_text(encoding="utf-8")
                    )
                    replay_http_status = self._extract_http_status(replay_response)
                    replay_response_content_type = self._extract_response_content_type(
                        replay_response
                    )
                    observed_mode_value = self._extract_response_field(
                        replay_response,
                        "responseMode",
                    )
                    replay_observed_response_mode = (
                        str(observed_mode_value)
                        if observed_mode_value is not None
                        else None
                    )
                except (OSError, json.JSONDecodeError) as exc:
                    warnings.append(f"replay response status: {str(exc)[:1000]}")
            try:
                response_page = await self.playwright.current_page(
                    session_id,
                    Deadline(1_500),
                )
                post_response_alignment = await self.js_reverse.align_page(
                    response_page,
                    Deadline(1_500),
                    page_id=(
                        str(session["js_reverse_page_id"])
                        if session.get("js_reverse_page_id")
                        else None
                    ),
                )
            except Exception as exc:
                warnings.append(f"post-response alignment: {str(exc)[:1000]}")
            step_results.append(
                FlowStepResult(
                    step_id="replay_request",
                    phase="replay",
                    status="completed",
                    started_at=started,
                    ended_at=utc_now(),
                )
            )
        except asyncio.CancelledError:
            step_results.append(
                FlowStepResult(
                    step_id="replay_request",
                    phase="replay",
                    status="canceled_outcome_unknown",
                    started_at=started,
                    ended_at=utc_now(),
                    error="Browser-context replay was canceled after dispatch.",
                )
            )
            raise
        except Exception as exc:
            step_results.append(
                FlowStepResult(
                    step_id="replay_request",
                    phase="replay",
                    status="failed",
                    started_at=started,
                    ended_at=utc_now(),
                    error=str(exc)[:4000],
                )
            )
            raise
        for path, suffix, kind, sensitivity, credentials in [
            (
                spec_file,
                "spec",
                "replay_request_spec",
                "credential",
                True,
            ),
            (
                diff_file,
                "diff",
                "replay_request_diff",
                "private",
                False,
            ),
            (
                result_file,
                "response",
                "replay_response",
                "private",
                False,
            ),
        ]:
            descriptor = self.experiments.describe_local_artifact(
                str(path),
                artifact_id=f"art_{experiment_id}_replay_{suffix}",
                kind=kind,
                sensitivity=sensitivity,
                contains_credentials=credentials,
            )
            if descriptor:
                replay_artifacts.append(descriptor)
        return ReplayDispatchResult(
            stream_checkpoint=stream_checkpoint,
            first_mutation_wall_time_ms=first_mutation_wall_time_ms,
            replay_result=replay_result,
            replay_response=replay_response,
            http_status=replay_http_status,
            response_content_type=replay_response_content_type,
            observed_response_mode=replay_observed_response_mode,
            post_response_alignment=post_response_alignment,
            artifacts=replay_artifacts,
        )
    async def _prepare_replay_dispatch_stage(
        self,
        *,
        replay_plan: dict[str, Any],
        manifest: dict[str, Any],
        experiment_id: str,
        experiment_dir: Path,
        session_id: str,
        session: dict[str, Any],
        deadline: Deadline,
        capture_id: int | None,
        request_matcher: RequestMatcher,
        stream_checkpoint: StreamCheckpoint,
        first_mutation_wall_time_ms: int | None,
        step_results: list[FlowStepResult],
        wait_observations: list[dict[str, Any]],
        alignment: AlignmentResult,
        warnings: list[str],
    ) -> ReplayPreparationResult:
        replay_artifacts: list[dict[str, Any]] = []
        pre_dispatch_alignment = alignment
        setup_steps = replay_plan.get("_setup_flow_steps")
        extractors = replay_plan.get("_extractors")
        extractor_checkpoint = (
            network_checkpoint(
                await self._all_network_requests(
                    self._operation_deadline(
                        deadline,
                        2_500,
                        "extractor network checkpoint",
                    )
                ),
                generation=self._transport_generation(),
            )
            if isinstance(extractors, list) and extractors
            else None
        )
        if isinstance(setup_steps, list) and setup_steps:
            (
                stream_checkpoint,
                first_mutation_wall_time_ms,
            ) = await StepExecutor.execute_many(
                self,
                phase="setup",
                steps=setup_steps,
                session_id=session_id,
                experiment_dir=experiment_dir,
                deadline=deadline,
                capture_id=capture_id,
                request_matcher=request_matcher,
                stream_checkpoint=stream_checkpoint,
                first_mutation_wall_time_ms=first_mutation_wall_time_ms,
                step_results=step_results,
                wait_observations=wait_observations,
            )
        if isinstance(extractor_checkpoint, dict):
            (
                extractor_values,
                extractor_records,
                extractor_artifacts,
            ) = await self._run_replay_extractors(
                extractors=extractors,
                checkpoint=extractor_checkpoint,
                experiment_dir=experiment_dir,
                deadline=self._operation_deadline(
                    deadline,
                    8_000,
                    "run replay extractors",
                ),
            )
            replay_artifacts.extend(extractor_artifacts)
            for binding in replay_plan["_binding_specs"]:
                if (
                    binding.value_source == "extractor"
                    and binding.extractor_id in extractor_values
                ):
                    replay_plan["binding_values"][binding.binding_id] = (
                        extractor_values[str(binding.extractor_id)]
                    )
            resolved_bindings = [
                item
                for item in replay_plan["_binding_specs"]
                if item.binding_id in replay_plan["binding_values"]
            ]
            unresolved = sorted(
                item.binding_id
                for item in replay_plan["_binding_specs"]
                if item.binding_id not in replay_plan["binding_values"]
            )
            rebuilt_spec, rebuilt_diff = build_replay_spec(
                replay_plan["_source_snapshot"],
                replay_plan["mutations"],
                bindings=resolved_bindings,
                binding_values=replay_plan["binding_values"],
                query_serialization=replay_plan["query_serialization"],
            )
            rebuilt_spec["responseControl"] = replay_plan["spec"]["responseControl"]
            rebuilt_spec["transport"] = replay_plan["spec"]["transport"]
            replay_plan["spec"] = rebuilt_spec
            replay_plan["diff"] = rebuilt_diff
            replay_plan["unresolved_binding_ids"] = unresolved
            replay_plan["binding_observations"] = self._binding_observations(
                replay_plan["_binding_specs"],
                replay_plan["binding_values"],
            )
            replay_plan["extractor_observations"] = extractor_records
            replay_plan["expected_request_body_canonical_sha256"] = (
                request_body_canonical_sha256_from_spec(rebuilt_spec)
            )
            replay_manifest = manifest.get("replay")
            if isinstance(replay_manifest, dict):
                replay_manifest.update(
                    {
                        "binding_observations": replay_plan[
                            "binding_observations"
                        ],
                        "unresolved_binding_ids": unresolved,
                        "extractor_observations": extractor_records,
                        "expected_request_body_canonical_sha256": replay_plan[
                            "expected_request_body_canonical_sha256"
                        ],
                    }
                )
                self.experiments.write_manifest(experiment_id, manifest)
        try:
            setup_page = await self.playwright.current_page(
                session_id,
                self._operation_deadline(
                    deadline,
                    2_500,
                    "pre-dispatch current page",
                ),
            )
            pre_dispatch_alignment = await self.js_reverse.align_page(
                setup_page,
                self._operation_deadline(
                    deadline,
                    2_500,
                    "pre-dispatch page alignment",
                ),
                page_id=(
                    str(session["js_reverse_page_id"])
                    if session.get("js_reverse_page_id")
                    else None
                ),
            )
        except Exception as exc:
            warnings.append(f"pre-dispatch alignment: {str(exc)[:1000]}")
        return ReplayPreparationResult(
            stream_checkpoint=stream_checkpoint,
            first_mutation_wall_time_ms=first_mutation_wall_time_ms,
            pre_dispatch_alignment=pre_dispatch_alignment,
            artifacts=replay_artifacts,
        )
