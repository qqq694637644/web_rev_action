"""Collector, trace, screenshot, and artifact finalization."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ...browser_models import BrowserActionResponse, CaptureFlowPayload, RequestMatcher
from ...protocol_evidence import (
    aggregate_observation_completeness,
    evidence_id,
    public_alignment_summary,
    public_network_request_summary,
)
from ..core import Deadline, utc_now
from .context import CaptureCompletionContext


class BrowserFinalizationOperations:
    """Own finalization behavior while the public service remains a facade."""

    async def _finalize_experiment_runtime(
        self,
        *,
        session_id: str,
        experiment_dir: Path,
        payload: CaptureFlowPayload,
        capture_id: int | None,
        stream_start_status: str,
        capture_transport_generation: int | None,
        trace_started: bool,
        execution_deadline: Deadline,
        canceled: bool,
    ) -> dict[str, Any]:
        cleanup_deadline = Deadline(self.FINALIZE_GRACE_MS)
        entered_reserve = execution_deadline.remaining_ms() <= self.FINALIZE_RESERVE_MS
        result: dict[str, Any] = {
            "stop_payload": {},
            "final_status_payload": {},
            "trace_paths": [],
            "screenshot_paths": [],
            "snapshot_paths": [],
            "network_payload": {},
            "collector_stopped": (
                not payload.capture.stream
                or stream_start_status
                in {"not_attempted", "failed_before_send"}
            ),
            "collector_cleanup": (
                "not_required"
                if not payload.capture.stream
                or stream_start_status
                in {"not_attempted", "failed_before_send"}
                else "unknown"
            ),
            "orphan_capture_id": None,
            "warnings": [],
            "errors": [],
            "entered_finalize_reserve": entered_reserve,
        }
        can_stop_live_capture = (
            capture_id is not None
            and stream_start_status
            in {"confirmed", "outcome_unknown", "failed_after_dispatch"}
            and capture_transport_generation == self._transport_generation()
        )
        if can_stop_live_capture:
            try:
                result["stop_payload"] = await self.js_reverse.stop_stream_capture(
                    capture_id,
                    cleanup_deadline.child(6_000),
                )
                result["collector_stopped"] = True
                result["collector_cleanup"] = "completed"
            except Exception as exc:
                result["errors"].append(f"stream stop: {str(exc)[:3500]}")
                result["orphan_capture_id"] = capture_id
                message = str(exc).lower()
                result["collector_cleanup"] = (
                    "timed_out" if "timed out" in message or "deadline" in message else "unknown"
                )
            else:
                if not canceled and cleanup_deadline.remaining_ms() > 500:
                    try:
                        result["final_status_payload"] = await self.js_reverse.get_stream_status(
                            capture_id,
                            cleanup_deadline.child(1_500),
                        )
                    except Exception as exc:
                        result["warnings"].append(f"post-stop status: {str(exc)[:3500]}")
                if not result["final_status_payload"] and result["stop_payload"]:
                    result["final_status_payload"] = dict(result["stop_payload"])
        elif payload.capture.stream and stream_start_status in {
            "confirmed",
            "outcome_unknown",
            "failed_after_dispatch",
        }:
            result["collector_stopped"] = False
            result["collector_cleanup"] = "unknown"
            if capture_id is not None:
                result["orphan_capture_id"] = capture_id
        if trace_started:
            try:
                result["trace_paths"] = await self.playwright.stop_trace(
                    session_id,
                    experiment_dir,
                    cleanup_deadline.child(1_500),
                    collect_files=not entered_reserve,
                )
            except Exception as exc:
                result["warnings"].append(f"trace finalize: {str(exc)[:3500]}")
        if not canceled and not entered_reserve and execution_deadline.remaining_ms() > 1_000:
            if payload.capture.network or payload.network_evidence:
                try:
                    result["network_payload"] = await self.js_reverse.list_network_requests(
                        RequestMatcher(),
                        execution_deadline.child(min(2_000, execution_deadline.remaining_ms())),
                    )
                except Exception as exc:
                    result["warnings"].append(f"network summary: {str(exc)[:3500]}")
            if payload.capture.screenshots and execution_deadline.remaining_ms() > 500:
                try:
                    result["screenshot_paths"].append(
                        await self.playwright.capture_screenshot(
                            session_id,
                            experiment_dir,
                            "after-flow",
                            execution_deadline.child(min(2_000, execution_deadline.remaining_ms())),
                        )
                    )
                except Exception as exc:
                    result["warnings"].append(f"final screenshot: {str(exc)[:3500]}")
            if payload.capture.page_snapshots and execution_deadline.remaining_ms() > 500:
                try:
                    result["snapshot_paths"].append(
                        await self.playwright.capture_snapshot(
                            session_id,
                            experiment_dir,
                            "after-flow",
                            execution_deadline.child(min(2_000, execution_deadline.remaining_ms())),
                        )
                    )
                except Exception as exc:
                    result["warnings"].append(f"final page snapshot: {str(exc)[:3500]}")
        return result

    async def _complete_capture_record(
        self,
        context: CaptureCompletionContext,
    ) -> BrowserActionResponse:
        operation = context.operation
        session_id = context.session_id
        experiment_id = context.experiment_id
        manifest = context.manifest
        payload = context.payload
        deadline = context.deadline
        alignment = context.alignment
        post_alignment = context.post_alignment
        step_results = context.step_results
        wait_result = context.wait_result
        wait_observations = context.wait_observations
        cancelled_error = context.cancelled_error
        errors = context.errors
        warnings = context.warnings
        replay_plan = context.replay_plan
        replay_result = context.replay_result
        replay_http_status = context.replay_http_status
        replay_response_content_type = context.replay_response_content_type
        mutation_assessment = context.mutation_assessment
        response_analysis = context.response_analysis
        stream_response_contract = context.stream_response_contract
        response_evidence_source = context.response_evidence_source
        replay_network_evidence_id = context.replay_network_evidence_id
        pre_dispatch_environment = context.pre_dispatch_environment
        post_response_environment = context.post_response_environment
        post_verification_environment = context.post_verification_environment
        comparison_results = context.comparison_results
        extractor_observations = context.extractor_observations
        network_observations = context.network_observations
        cancellation_classifications = context.cancellation_classifications
        primary_requests = context.primary_requests
        observed_stream_response = context.observed_stream_response
        non_stream_error_response_observed = context.non_stream_error_response_observed
        stream_evidence_required = context.stream_evidence_required
        collector_started = context.collector_started
        collector_stopped = context.collector_stopped
        collector_start_wall_time_ms = context.collector_start_wall_time_ms
        first_mutation_wall_time_ms = context.first_mutation_wall_time_ms
        cleanup_result = context.cleanup_result
        capture_id = context.capture_id
        capture_uuid = context.capture_uuid
        capture_relative_dir = context.capture_relative_dir
        capture_metadata_artifact_id = context.capture_metadata_artifact_id
        capture_transport_generation = context.capture_transport_generation
        stream_start_status = context.stream_start_status
        start_payload = context.start_payload
        final_status_payload = context.final_status_payload
        stop_payload = context.stop_payload
        network_payload = context.network_payload
        network_checkpoint_value = context.network_checkpoint_value
        console_checkpoint_value = context.console_checkpoint_value
        evidence_entries = context.evidence_entries
        evidence_artifacts = context.evidence_artifacts
        replay_artifacts = context.replay_artifacts
        screenshot_paths = context.screenshot_paths
        snapshot_paths = context.snapshot_paths
        trace_paths = context.trace_paths
        count_ok = context.count_satisfied
        response_analysis_summary: dict[str, Any] | None = None
        capture_summary = (
            final_status_payload.get("capture")
            if isinstance(final_status_payload.get("capture"), dict)
            else {}
        )
        collector_integrity = str(
            "not_required"
            if not payload.capture.stream
            else capture_summary.get("collectorIntegrity")
            or capture_summary.get("integrityStatus")
            or ("partial" if collector_started else "failed")
        )
        wait_met = wait_result is None or bool(wait_result.get("condition_met"))
        steps_ok = all(item.status == "completed" for item in step_results)
        execution_failed = (
            cancelled_error is not None
            or not steps_ok
            or not wait_met
            or (payload.capture.stream and not collector_stopped)
            or bool(errors)
        )
        required_dimensions: set[str] = set()
        if payload.primary_request.expected_min_matches > 0:
            if (
                (
                    payload.requirements.require_raw_capture
                    or (replay_plan is not None and observed_stream_response)
                )
                and not non_stream_error_response_observed
            ):
                required_dimensions.add("raw_stream")
            if (
                (
                    payload.requirements.require_semantic_parse
                    or (
                        observed_stream_response
                        and replay_plan is not None
                        and not bool(
                            replay_plan.get("replay_protocol", {})
                            .get("response_reader", {})
                            .get("raw_only")
                        )
                    )
                )
                and not non_stream_error_response_observed
            ):
                required_dimensions.add("semantic_stream")
            if payload.requirements.require_request_snapshot:
                required_dimensions.update({"request_headers", "request_body"})
            if payload.requirements.require_artifacts:
                if stream_evidence_required:
                    required_dimensions.add("stream_artifacts")
                else:
                    required_dimensions.add("network_artifacts")
            if non_stream_error_response_observed:
                required_dimensions.update({"response_headers", "response_body"})
        observation_dimensions, missing_evidence = aggregate_observation_completeness(
            network_observations,
            required_dimensions=required_dimensions,
        )
        if (
            replay_plan is not None
            and not non_stream_error_response_observed
            and isinstance(stream_response_contract, dict)
        ):
            terminal_status = str(stream_response_contract.get("status") or "partial")
            observation_dimensions["stream_terminal_contract"] = terminal_status
            if terminal_status != "complete":
                missing_evidence.append("stream_terminal_contract")
        required_values = list(observation_dimensions.values())
        evidence_errors: list[str] = []
        for observation in extractor_observations:
            if observation.get("required") is True and observation.get("status") != "completed":
                extractor_id = str(observation.get("extractor_id") or "unknown")
                evidence_errors.append(f"required_extractor_failed:{extractor_id}")
                missing_evidence.append(f"extractor:{extractor_id}")
        if not count_ok:
            evidence_errors.append("observation_count_out_of_range")
            missing_evidence.append("observation_count")
        for name, value in observation_dimensions.items():
            if value == "failed":
                evidence_errors.append(f"required_completeness_failed:{name}")
        network_backed_dimensions = {
            "request_headers",
            "request_body",
            "response_headers",
            "response_body",
            "network_artifacts",
        }
        if required_dimensions.intersection(network_backed_dimensions):
            for observation in network_observations:
                association = observation.get("association")
                association = association if isinstance(association, dict) else {}
                if association.get("confidence") in {"ambiguous", "missing"}:
                    observation_id = str(observation.get("observation_id") or "unknown")
                    evidence_errors.append(f"network_association_failed:{observation_id}")
                    missing_evidence.append(f"association:{observation_id}")
        if (
            stream_evidence_required
            and not payload.primary_request.allow_supporting_failures
            and collector_integrity != "complete"
        ):
            missing_evidence.append("collector")
            if collector_integrity == "failed":
                evidence_errors.append("collector_failed")
        missing_evidence = sorted(set(missing_evidence))
        evidence_errors = sorted(set(evidence_errors))
        evidence_failed = bool(evidence_errors) or any(
            value == "failed" for value in required_values
        )
        evidence_partial = not evidence_failed and (
            any(value != "complete" for value in required_values)
            or (
                stream_evidence_required
                and not payload.primary_request.allow_supporting_failures
                and collector_integrity != "complete"
            )
        )
        execution_integrity = "failed" if execution_failed else "complete"
        evidence_integrity = (
            "failed" if evidence_failed else "partial" if evidence_partial else "complete"
        )
        quality_summary = {
            "status": evidence_integrity,
            "observation_count": len(network_observations),
            "expected_observation_count": {
                "min": payload.primary_request.expected_min_matches,
                "max": payload.primary_request.expected_max_matches,
            },
            "count_satisfied": count_ok,
            "required_completeness": observation_dimensions,
            "missing_evidence": missing_evidence,
            "errors": evidence_errors,
        }
        response_status = (
            "interrupted"
            if cancelled_error is not None
            else "failed"
            if "failed" in {execution_integrity, evidence_integrity}
            else "partial"
            if "partial" in {execution_integrity, evidence_integrity}
            else "completed"
        )
        pre_arm_request_count = sum(
            1 for item in primary_requests if bool(item.get("requestStartedBeforeCapture"))
        )
        collector_before_mutation = (
            None
            if first_mutation_wall_time_ms is None
            else collector_start_wall_time_ms is not None
            and collector_start_wall_time_ms <= first_mutation_wall_time_ms
        )
        capture_health = {
            "page_aligned_before_flow": alignment.status == "aligned",
            "page_aligned_after_flow": post_alignment.status == "aligned",
            "collector_start_wall_time_ms": collector_start_wall_time_ms,
            "first_mutation_wall_time_ms": first_mutation_wall_time_ms,
            "collector_started_before_first_mutation": collector_before_mutation,
            "include_in_flight_requested": payload.primary_request.include_in_flight,
            "pre_arm_request_count": pre_arm_request_count,
            "primary_request_match_count_ok": count_ok,
            "wait_condition_met": wait_met,
            "collector_stopped": collector_stopped or not payload.capture.stream,
            "collector_cleanup": cleanup_result.get(
                "collector_cleanup",
                "not_required" if not payload.capture.stream else "unknown",
            ),
            "orphan_capture_id": cleanup_result.get("orphan_capture_id"),
            "capture_uuid": capture_uuid,
            "capture_relative_dir": capture_relative_dir,
            "capture_metadata_artifact_id": capture_metadata_artifact_id,
            "capture_namespace": experiment_id,
            "stream_start_status": stream_start_status,
            "transport_generation": capture_transport_generation,
            "entered_finalize_reserve": cleanup_result.get("entered_finalize_reserve", False),
            "capture_scope": capture_summary.get("captureScope", "page-target-only"),
            "worker_coverage": capture_summary.get("workerCoverage", False),
        }
        artifacts = self._collect_artifacts(
            start_payload,
            final_status_payload,
            stop_payload,
            network_payload,
        )
        artifacts.extend(evidence_artifacts)
        artifacts.extend(replay_artifacts)
        for index, screenshot_path in enumerate(screenshot_paths, start=1):
            descriptor = self.experiments.describe_local_artifact(
                screenshot_path,
                artifact_id=f"art_{experiment_id}_screenshot_{index}",
                kind="playwright_screenshot",
                sensitivity="private",
            )
            if descriptor:
                artifacts.append(descriptor)
                evidence_entries.append(
                    {
                        "evidence_id": evidence_id(
                            experiment_id,
                            "page_screenshot",
                            stable_id=index,
                        ),
                        "kind": "page_screenshot",
                        "artifact_ids": [descriptor["artifactId"]],
                        "artifact_paths": {"screenshot": descriptor["relativePath"]},
                    }
                )
        for index, snapshot_path in enumerate(snapshot_paths, start=1):
            descriptor = self.experiments.describe_local_artifact(
                snapshot_path,
                artifact_id=f"art_{experiment_id}_page_snapshot_{index}",
                kind="playwright_page_snapshot",
                sensitivity="private",
            )
            if descriptor:
                artifacts.append(descriptor)
                evidence_entries.append(
                    {
                        "evidence_id": evidence_id(
                            experiment_id,
                            "page_snapshot",
                            stable_id=index,
                        ),
                        "kind": "page_snapshot",
                        "artifact_ids": [descriptor["artifactId"]],
                        "artifact_paths": {"snapshot": descriptor["relativePath"]},
                    }
                )
        for index, trace_path in enumerate(trace_paths, start=1):
            descriptor = self.experiments.describe_local_artifact(
                trace_path,
                artifact_id=f"art_{experiment_id}_trace_{index}",
                kind="playwright_trace",
                sensitivity="private",
            )
            if descriptor:
                artifacts.append(descriptor)
        for artifact in artifacts:
            write_status = artifact.get("writeStatus") or artifact.get("write_status")
            relative_path = artifact.get("relativePath") or artifact.get("relative_path")
            if write_status not in {None, "written"}:
                artifact["completeness"] = "failed"
            elif relative_path:
                artifact["completeness"] = "complete"
            else:
                artifact["completeness"] = "partial"
        relative_screenshot_paths = [
            relative
            for path in screenshot_paths
            if (relative := self.experiments.relative_path(path)) is not None
        ]
        relative_snapshot_paths = [
            relative
            for path in snapshot_paths
            if (relative := self.experiments.relative_path(path)) is not None
        ]
        relative_trace_paths = [
            relative
            for path in trace_paths
            if (relative := self.experiments.relative_path(path)) is not None
        ]
        if replay_plan is not None:
            replay_attempt_evidence_id = evidence_id(
                experiment_id,
                "replay_attempt",
                stable_id=replay_plan["replay_attempt_id"],
            )
            if isinstance(response_analysis, dict):
                analyzer = response_analysis.get("analyzer")
                analyzer = analyzer if isinstance(analyzer, dict) else {}
                name = analyzer.get("name")
                version = analyzer.get("version")
                response_analysis_summary = {
                    "analyzer": (
                        f"{name}@{version}"
                        if name is not None and version is not None
                        else None
                    ),
                    "classification": response_analysis.get("classification"),
                    "evidence_id": replay_attempt_evidence_id,
                }
            replay_transport_semantics = {
                **(
                    replay_plan.get("transport")
                    if isinstance(replay_plan.get("transport"), dict)
                    else {}
                ),
                "source_fetch_options_known": False,
            }
            replay_manifest = manifest.get("replay")
            if isinstance(replay_manifest, dict):
                replay_manifest.update(
                    {
                        "network_evidence_id": replay_network_evidence_id,
                        "dispatch_wall_time_ms": replay_plan.get("dispatch_wall_time_ms"),
                        "replay_http_status": replay_http_status,
                        "mutation_assessment": mutation_assessment,
                        "stream_response_contract": stream_response_contract,
                        "response_evidence_source": response_evidence_source,
                        "pre_dispatch_environment": pre_dispatch_environment,
                        "post_response_environment": post_response_environment,
                        "post_verification_environment": (post_verification_environment),
                        "comparison_results": comparison_results,
                        "transport_semantics": replay_transport_semantics,
                        **(
                            {"response_analysis_evidence_id": replay_attempt_evidence_id}
                            if response_analysis is not None
                            else {}
                        ),
                    }
                )
            replay_artifact_ids = [
                str(item.get("artifactId"))
                for item in replay_artifacts
                if item.get("artifactId")
            ]
            evidence_entries.append(
                {
                    "evidence_id": replay_attempt_evidence_id,
                    "kind": "replay_attempt",
                    "replay_attempt_id": replay_plan["replay_attempt_id"],
                    "replay_protocol_hash": replay_plan["replay_protocol_hash"],
                    "requested_replay_protocol_hash": replay_plan[
                        "requested_replay_protocol_hash"
                    ],
                    "source_experiment_id": replay_plan["source_experiment_id"],
                    "source_evidence_id": replay_plan["source_evidence_id"],
                    "network_evidence_id": replay_network_evidence_id,
                    "mutation_assessment": mutation_assessment,
                    "stream_response_contract": stream_response_contract,
                    "response_evidence_source": response_evidence_source,
                    "pre_dispatch_environment": pre_dispatch_environment,
                    "post_response_environment": post_response_environment,
                    "post_verification_environment": (post_verification_environment),
                    "comparison_results": comparison_results,
                    "transport_semantics": replay_transport_semantics,
                    "artifact_ids": replay_artifact_ids,
                    "step_ids": ["replay_request"],
                    **(
                        {"response_analysis": response_analysis}
                        if response_analysis is not None
                        else {}
                    ),
                    "summary": {
                        "http_status": replay_http_status,
                        "response_content_type": replay_response_content_type,
                        "non_stream_error_response_observed": (
                            non_stream_error_response_observed
                        ),
                        **{
                            key: replay_result.get(key)
                            for key in (
                                "resultType",
                                "filename",
                                "byteLength",
                                "charLength",
                                "truncated",
                            )
                            if key in replay_result
                        },
                    },
                }
            )
        public_network_payload = {
            **network_payload,
            "requests": [
                public_network_request_summary(item)
                for item in network_payload.get("requests", [])
                if isinstance(item, dict)
            ],
        }
        manifest.update(
            {
                "status": response_status,
                "deadline": deadline.to_dict(),
                "steps": [item.model_dump(mode="json") for item in step_results],
                "stream_capture_id": capture_id,
                "stream_status": final_status_payload,
                "stream_runtime": {
                    "start_status": stream_start_status,
                    "capture_id": capture_id,
                    "capture_uuid": capture_uuid,
                    "capture_relative_dir": capture_relative_dir,
                    "capture_metadata_artifact_id": (capture_metadata_artifact_id),
                    "transport_generation": capture_transport_generation,
                    "capture_namespace": experiment_id,
                },
                "stream_wait_result": wait_result,
                "wait_observations": wait_observations,
                "execution": {
                    "status": execution_integrity,
                    "errors": errors,
                },
                "quality_summary": quality_summary,
                "analysis_warnings": warnings,
                "comparison_results": comparison_results,
                "objective_requirements": payload.requirements.model_dump(mode="json"),
                "network_observations": network_observations,
                "cancellation_classifications": cancellation_classifications,
                "post_flow_alignment": public_alignment_summary(post_alignment),
                "capture_health": capture_health,
                "network_checkpoint": network_checkpoint_value,
                "network_summary": public_network_payload,
                "console_checkpoint": console_checkpoint_value,
                "screenshot_paths": relative_screenshot_paths,
                "snapshot_paths": relative_snapshot_paths,
                "trace_paths": relative_trace_paths,
                "replay_result": replay_result,
                "replay_http_status": replay_http_status,
                "replay_response_content_type": replay_response_content_type,
                "stream_response_contract": stream_response_contract,
                "response_evidence_source": response_evidence_source,
                "replay_attempt_id": (
                    replay_plan["replay_attempt_id"] if replay_plan is not None else None
                ),
                "replay_protocol_hash": (
                    replay_plan["replay_protocol_hash"] if replay_plan is not None else None
                ),
                "requested_replay_protocol_hash": (
                    replay_plan["requested_replay_protocol_hash"]
                    if replay_plan is not None
                    else None
                ),
                "pre_dispatch_environment": pre_dispatch_environment,
                "post_response_environment": post_response_environment,
                "post_verification_environment": post_verification_environment,
                "replay_transport_semantics": (
                    replay_transport_semantics if replay_plan is not None else None
                ),
                "non_stream_error_response_observed": (non_stream_error_response_observed),
                **(
                    {"response_analysis_summary": response_analysis_summary}
                    if response_analysis_summary is not None
                    else {}
                ),
                "mutation_assessment": mutation_assessment,
                "evidence": evidence_entries,
                "artifacts": artifacts,
                "warnings": warnings,
                "errors": errors,
            }
        )
        if cancelled_error is not None:
            manifest["interrupted_at"] = utc_now()
            write_task = asyncio.create_task(
                asyncio.to_thread(
                    self.experiments.write_manifest,
                    experiment_id,
                    manifest,
                )
            )
            await asyncio.shield(write_task)
            raise cancelled_error
        self.experiments.write_manifest(experiment_id, manifest)
        return BrowserActionResponse(
            operation=operation,
            status=response_status,
            session_id=session_id,
            experiment_id=experiment_id,
            result={
                "experiment": self._experiment_summary(manifest),
                "manifest_relative_path": self._manifest_relative_path(experiment_id),
            },
            warnings=warnings,
            errors=errors,
        )
