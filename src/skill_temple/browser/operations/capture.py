"""Capture orchestration with explicit stage dependencies."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ...browser_models import (
    BrowserActionResponse,
    CaptureBaselineRequest,
    CaptureFlowPayload,
    CaptureFlowRequest,
    FlowStepResult,
    ReplayRequestRequest,
)
from ...protocol.matching import (
    network_checkpoint,
)
from ..adapters.contracts import AlignmentResult, McpToolCallError, StreamCheckpoint
from ..core import BrowserServiceError, Deadline, utc_now
from ..steps import StepExecutor
from .context import CaptureCompletionContext


class BrowserCaptureOperations:
    """Own capture behavior while the public service remains a facade."""

    def _start_capture_job(
        self,
        request: CaptureFlowRequest | CaptureBaselineRequest | ReplayRequestRequest,
        *,
        experiment_id: str,
        payload: CaptureFlowPayload,
        replay_plan: dict[str, Any] | None,
    ) -> BrowserActionResponse:
        session = self._get_session(payload.session_id)
        if session.get("status") != "open":
            raise BrowserServiceError("session_closed", "Browser session is not open", 409)
        deadline = Deadline(payload.job_timeout_ms)
        experiment_id, experiment_dir, manifest = self.experiments.create_experiment(
            session_id=payload.session_id,
            operation=request.operation,
            objective=payload.objective,
            deadline=deadline,
            experiment_id=experiment_id,
        )
        manifest.update(
            {
                "execution_mode": "job",
                "job_timeout_ms": payload.job_timeout_ms,
                "primary_request_matcher": payload.primary_request.model_dump(
                    mode="json", exclude_none=True
                ),
            }
        )
        self._validate_and_store_series(
            session_id=payload.session_id,
            manifest=manifest,
            payload=payload,
        )
        if replay_plan:
            manifest["replay_source"] = {
                "source_experiment_id": replay_plan["source_experiment_id"],
                "source_evidence_id": replay_plan["source_evidence_id"],
            }
            manifest["replay"] = self._replay_manifest_seed(replay_plan)
        self.experiments.write_manifest(experiment_id, manifest)
        task = asyncio.create_task(
            self._run_capture_job(
                request,
                deadline=deadline,
                prepared=(experiment_id, experiment_dir, manifest),
                payload=payload,
                replay_plan=replay_plan,
            ),
            name=f"browser-experiment-{experiment_id}",
        )
        self._jobs[experiment_id] = task
        self._active_session_jobs[payload.session_id] = experiment_id

        def clear_job(_task: asyncio.Task[None]) -> None:
            self._jobs.pop(experiment_id, None)
            if self._active_session_jobs.get(payload.session_id) == experiment_id:
                self._active_session_jobs.pop(payload.session_id, None)

        task.add_done_callback(clear_job)
        return BrowserActionResponse(
            operation=request.operation,
            status="running",
            session_id=payload.session_id,
            experiment_id=experiment_id,
            result={
                "experiment": self._experiment_summary(manifest),
                "manifest_relative_path": self._manifest_relative_path(experiment_id),
                "poll_with": "inspectBrowserEvidence.get_experiment",
            },
        )

    async def _run_capture_job(
        self,
        request: CaptureFlowRequest | CaptureBaselineRequest | ReplayRequestRequest,
        *,
        deadline: Deadline,
        prepared: tuple[str, Path, dict[str, Any]],
        payload: CaptureFlowPayload,
        replay_plan: dict[str, Any] | None,
    ) -> None:
        experiment_id = prepared[0]
        try:
            try:
                await self._capture_flow(
                    request,
                    deadline=deadline,
                    prepared=prepared,
                    payload=payload,
                    replay_plan=replay_plan,
                )
            except asyncio.CancelledError:
                manifest = self.experiments.load_manifest(experiment_id)
                manifest["status"] = "interrupted"
                manifest["errors"] = [
                    *(manifest.get("errors") if isinstance(manifest.get("errors"), list) else []),
                    "Background experiment task was canceled.",
                ]
                self.experiments.write_manifest(experiment_id, manifest)
                raise
            except Exception as exc:
                manifest = self.experiments.load_manifest(experiment_id)
                manifest["status"] = "failed"
                manifest["errors"] = [
                    *(manifest.get("errors") if isinstance(manifest.get("errors"), list) else []),
                    str(exc)[:4000],
                ]
                self.experiments.write_manifest(experiment_id, manifest)
        finally:
            await self._release_browser_operation(experiment_id)

    async def wait_for_job(self, experiment_id: str) -> None:
        task = self._jobs.get(experiment_id)
        if task is not None:
            await task

    async def _capture_flow(
        self,
        request: CaptureFlowRequest | CaptureBaselineRequest | ReplayRequestRequest,
        *,
        deadline: Deadline | None = None,
        prepared: tuple[str, Path, dict[str, Any]] | None = None,
        payload: CaptureFlowPayload | None = None,
        replay_plan: dict[str, Any] | None = None,
    ) -> BrowserActionResponse:
        if payload is None:
            if isinstance(request, ReplayRequestRequest):
                payload, replay_plan = self._prepare_replay_execution(request)
            else:
                payload = request.payload
        deadline = deadline or Deadline(payload.deadline_ms)
        session_id = payload.session_id
        if prepared is None:
            experiment_id, experiment_dir, manifest = self.experiments.create_experiment(
                session_id=session_id,
                operation=request.operation,
                objective=payload.objective,
                deadline=deadline,
            )
            manifest["execution_mode"] = "sync"
            manifest["primary_request_matcher"] = payload.primary_request.model_dump(
                mode="json", exclude_none=True
            )
            self.experiments.write_manifest(experiment_id, manifest)
        else:
            experiment_id, experiment_dir, manifest = prepared
        async with self._locked_browser_session(session_id, deadline):
            session = self._get_session(session_id)
            if session.get("status") != "open":
                manifest["status"] = "failed"
                manifest["errors"] = ["Browser session is not open."]
                self.experiments.write_manifest(experiment_id, manifest)
                return BrowserActionResponse(
                    operation=request.operation,
                    status="failed",
                    session_id=session_id,
                    experiment_id=experiment_id,
                    result={
                        "experiment": self._experiment_summary(manifest),
                        "manifest_relative_path": self._manifest_relative_path(experiment_id),
                    },
                    errors=manifest["errors"],
                )
            try:
                alignment = await self._align_session(session, payload, deadline)
            except asyncio.CancelledError as exc:
                manifest["status"] = "interrupted"
                manifest["errors"] = [
                    "Experiment was canceled during page alignment before flow execution."
                ]
                manifest["interrupted_at"] = utc_now()
                manifest["updated_at"] = utc_now()
                write_task = asyncio.create_task(
                    asyncio.to_thread(
                        self.experiments.write_manifest,
                        experiment_id,
                        manifest,
                    )
                )
                await asyncio.shield(write_task)
                raise exc
            except Exception as exc:
                manifest["status"] = "failed"
                manifest["errors"] = [str(exc)[:4000]]
                self.experiments.write_manifest(experiment_id, manifest)
                return BrowserActionResponse(
                    operation=request.operation,
                    status="failed",
                    session_id=session_id,
                    experiment_id=experiment_id,
                    result={
                        "experiment": self._experiment_summary(manifest),
                        "manifest_relative_path": self._manifest_relative_path(experiment_id),
                    },
                    errors=manifest["errors"],
                )
            manifest["page_alignment"] = asdict(alignment)
            manifest["primary_request_matcher"] = payload.primary_request.model_dump(
                mode="json", exclude_none=True
            )
            capture_id: int | None = None
            capture_uuid: str | None = None
            capture_relative_dir: str | None = None
            capture_metadata_artifact_id: str | None = None
            capture_transport_generation: int | None = None
            stream_start_status = "not_attempted"
            start_payload: dict[str, Any] = {}
            final_status_payload: dict[str, Any] = {}
            stop_payload: dict[str, Any] = {}
            wait_result: dict[str, Any] | None = None
            trace_paths: list[str] = []
            screenshot_paths: list[str] = []
            snapshot_paths: list[str] = []
            network_payload: dict[str, Any] = {}
            network_checkpoint_value: dict[str, Any] = {}
            console_checkpoint_value: dict[str, Any] = {}
            replay_result: dict[str, Any] = {}
            replay_response: Any = None
            replay_http_status: int | None = None
            replay_response_content_type: str | None = None
            replay_observed_response_mode: str | None = None
            post_response_alignment: AlignmentResult | None = None
            pre_dispatch_alignment: AlignmentResult = alignment
            replay_artifacts: list[dict[str, Any]] = []
            step_results: list[FlowStepResult] = []
            wait_observations: list[dict[str, Any]] = []
            errors: list[str] = []
            warnings = list(alignment.warnings)
            trace_started = False
            collector_started = False
            collector_stopped = False
            stream_checkpoint = StreamCheckpoint()
            request_matcher = self._request_matcher(payload)
            collector_start_wall_time_ms: int | None = None
            first_mutation_wall_time_ms: int | None = None
            cancelled_error: asyncio.CancelledError | None = None
            cleanup_result: dict[str, Any] = {}
            try:
                if payload.capture.network or payload.network_evidence:
                    try:
                        checkpoint_requests = await self._all_network_requests(
                            self._operation_deadline(
                                deadline,
                                2_000,
                                "network checkpoint",
                            )
                        )
                        network_checkpoint_value = network_checkpoint(
                            checkpoint_requests,
                            generation=self._transport_generation(),
                        )
                    except Exception as exc:
                        warnings.append(f"network checkpoint: {str(exc)[:2000]}")
                if payload.capture.console_errors:
                    try:
                        console_checkpoint_value = await self._console_checkpoint(
                            self._operation_deadline(
                                deadline,
                                2_000,
                                "console checkpoint",
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"console checkpoint: {str(exc)[:2000]}")
                if payload.capture.trace:
                    await self.playwright.start_trace(
                        session_id,
                        self._operation_deadline(deadline, 3_000, "trace start"),
                    )
                    trace_started = True
                if payload.capture.stream:
                    stream_start_status = "failed_before_send"
                    try:
                        start_payload = await self.js_reverse.start_stream_capture(
                            experiment_id=experiment_id,
                            matcher=request_matcher,
                            include_in_flight=payload.primary_request.include_in_flight,
                            deadline=self._operation_deadline(
                                deadline,
                                5_000,
                                "stream capture start",
                            ),
                        )
                    except asyncio.CancelledError as exc:
                        capture_transport_generation = int(
                            getattr(
                                exc,
                                "mcp_transport_generation",
                                self._transport_generation(),
                            )
                        )
                        stream_start_status = (
                            "outcome_unknown"
                            if bool(getattr(exc, "mcp_outcome_unknown", False))
                            else "failed_before_send"
                        )
                        discovered = (
                            self._discover_capture_metadata(experiment_id)
                            if stream_start_status == "outcome_unknown"
                            else None
                        )
                        if discovered:
                            capture_id = (
                                int(discovered["capture_id"])
                                if isinstance(discovered.get("capture_id"), int)
                                else None
                            )
                            capture_uuid = discovered.get("capture_uuid")
                            capture_relative_dir = discovered.get("capture_relative_dir")
                            capture_metadata_artifact_id = discovered.get(
                                "capture_metadata_artifact_id"
                            )
                        self._write_stream_runtime(
                            experiment_id=experiment_id,
                            manifest=manifest,
                            start_status=stream_start_status,
                            capture_id=capture_id,
                            capture_uuid=(str(capture_uuid) if capture_uuid is not None else None),
                            capture_relative_dir=(
                                str(capture_relative_dir)
                                if capture_relative_dir is not None
                                else None
                            ),
                            capture_metadata_artifact_id=(
                                str(capture_metadata_artifact_id)
                                if capture_metadata_artifact_id is not None
                                else None
                            ),
                            transport_generation=capture_transport_generation,
                        )
                        raise
                    except McpToolCallError as exc:
                        capture_transport_generation = exc.transport_generation
                        stream_start_status = (
                            "outcome_unknown" if exc.outcome_unknown else "failed_before_send"
                        )
                        discovered = (
                            self._discover_capture_metadata(experiment_id)
                            if stream_start_status == "outcome_unknown"
                            else None
                        )
                        if discovered:
                            capture_id = (
                                int(discovered["capture_id"])
                                if isinstance(discovered.get("capture_id"), int)
                                else None
                            )
                            capture_uuid = discovered.get("capture_uuid")
                            capture_relative_dir = discovered.get("capture_relative_dir")
                            capture_metadata_artifact_id = discovered.get(
                                "capture_metadata_artifact_id"
                            )
                        self._write_stream_runtime(
                            experiment_id=experiment_id,
                            manifest=manifest,
                            start_status=stream_start_status,
                            capture_id=capture_id,
                            capture_uuid=(str(capture_uuid) if capture_uuid is not None else None),
                            capture_relative_dir=(
                                str(capture_relative_dir)
                                if capture_relative_dir is not None
                                else None
                            ),
                            capture_metadata_artifact_id=(
                                str(capture_metadata_artifact_id)
                                if capture_metadata_artifact_id is not None
                                else None
                            ),
                            transport_generation=capture_transport_generation,
                        )
                        raise
                    capture = start_payload.get("capture")
                    if not isinstance(capture, dict) or not capture.get("captureId"):
                        raise BrowserServiceError(
                            "stream_start_invalid", "Stream collector returned no capture ID", 502
                        )
                    capture_id = int(capture["captureId"])
                    capture_transport_generation = self._transport_generation()
                    stream_start_status = "confirmed"
                    capture_uuid = (
                        str(capture["captureUuid"]) if capture.get("captureUuid") else None
                    )
                    capture_relative_dir = (
                        str(capture["relativeDir"]) if capture.get("relativeDir") else None
                    )
                    metadata_artifact = capture.get("metadataArtifact")
                    if isinstance(metadata_artifact, dict) and metadata_artifact.get("artifactId"):
                        capture_metadata_artifact_id = str(metadata_artifact["artifactId"])
                    collector_started = True
                    collector_start_wall_time_ms = int(
                        capture.get("captureArmedWallTimeMs") or time.time() * 1000
                    )
                    stream_checkpoint = await self._stream_checkpoint(
                        capture_id,
                        request_matcher,
                        self._operation_deadline(
                            deadline,
                            1_500,
                            "initial stream checkpoint",
                        ),
                    )
                    self._write_stream_runtime(
                        experiment_id=experiment_id,
                        manifest=manifest,
                        start_status=stream_start_status,
                        capture_id=capture_id,
                        capture_uuid=capture_uuid,
                        capture_relative_dir=capture_relative_dir,
                        capture_metadata_artifact_id=capture_metadata_artifact_id,
                        transport_generation=capture_transport_generation,
                    )
                if replay_plan is not None:
                    replay_preparation = await self._prepare_replay_dispatch_stage(
                        replay_plan=replay_plan,
                        manifest=manifest,
                        experiment_id=experiment_id,
                        experiment_dir=experiment_dir,
                        session_id=session_id,
                        session=session,
                        deadline=deadline,
                        capture_id=capture_id,
                        request_matcher=request_matcher,
                        stream_checkpoint=stream_checkpoint,
                        first_mutation_wall_time_ms=first_mutation_wall_time_ms,
                        step_results=step_results,
                        wait_observations=wait_observations,
                        alignment=alignment,
                        warnings=warnings,
                    )
                    stream_checkpoint = replay_preparation.stream_checkpoint
                    first_mutation_wall_time_ms = (
                        replay_preparation.first_mutation_wall_time_ms
                    )
                    pre_dispatch_alignment = (
                        replay_preparation.pre_dispatch_alignment
                    )
                    replay_artifacts.extend(replay_preparation.artifacts)
                if payload.capture.screenshots:
                    try:
                        screenshot_paths.append(
                            await self.playwright.capture_screenshot(
                                session_id,
                                experiment_dir,
                                "before-flow",
                                self._operation_deadline(
                                    deadline,
                                    3_000,
                                    "initial screenshot",
                                ),
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"initial screenshot: {str(exc)[:3500]}")
                if payload.capture.page_snapshots:
                    try:
                        snapshot_paths.append(
                            await self.playwright.capture_snapshot(
                                session_id,
                                experiment_dir,
                                "before-flow",
                                self._operation_deadline(
                                    deadline,
                                    3_000,
                                    "initial page snapshot",
                                ),
                            )
                        )
                    except Exception as exc:
                        warnings.append(f"initial page snapshot: {str(exc)[:3500]}")
                if replay_plan is not None:
                    replay_dispatch = await self._execute_replay_dispatch(
                        experiment_id=experiment_id,
                        experiment_dir=experiment_dir,
                        manifest=manifest,
                        replay_plan=replay_plan,
                        session_id=session_id,
                        session=session,
                        deadline=deadline,
                        capture_id=capture_id,
                        request_matcher=request_matcher,
                        stream_checkpoint=stream_checkpoint,
                        first_mutation_wall_time_ms=first_mutation_wall_time_ms,
                        step_results=step_results,
                        warnings=warnings,
                    )
                    stream_checkpoint = replay_dispatch.stream_checkpoint
                    first_mutation_wall_time_ms = (
                        replay_dispatch.first_mutation_wall_time_ms
                    )
                    replay_result = replay_dispatch.replay_result
                    replay_response = replay_dispatch.replay_response
                    replay_http_status = replay_dispatch.http_status
                    replay_response_content_type = replay_dispatch.response_content_type
                    replay_observed_response_mode = (
                        replay_dispatch.observed_response_mode
                    )
                    post_response_alignment = replay_dispatch.post_response_alignment
                    replay_artifacts.extend(replay_dispatch.artifacts)
                (
                    stream_checkpoint,
                    first_mutation_wall_time_ms,
                ) = await StepExecutor.execute_many(
                    self,
                    phase=("verification" if replay_plan is not None else "action"),
                    steps=payload.flow,
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
                if payload.wait_for:
                    wait_deadline = self._operation_deadline(
                        deadline,
                        payload.wait_for.timeout_ms,
                        "final wait condition",
                    )
                    wait_result = await self._wait_condition(
                        session_ref=session_id,
                        capture_id=capture_id,
                        condition=payload.wait_for,
                        checkpoint=stream_checkpoint,
                        deadline=wait_deadline,
                    )
                    stream_checkpoint = self._checkpoint_from_wait_result(
                        wait_result,
                        stream_checkpoint,
                    )
                    wait_observations.append(
                        {
                            "step_id": "__final_wait__",
                            "step_index": len(payload.flow),
                            "condition_type": payload.wait_for.type,
                            "capture_version": wait_result.get("capture_version"),
                            "matched_request_ids": wait_result.get("matched_request_ids", []),
                            "matched_event": wait_result.get("matched_event"),
                            "terminal_status": wait_result.get("terminal_status"),
                        }
                    )
                    final_status_payload = dict(wait_result.get("status_payload") or {})
            except asyncio.CancelledError as exc:
                cancelled_error = exc
                errors.append("Experiment task was canceled; finalization was attempted.")
            except Exception as exc:
                errors.append(str(exc)[:4000])
            finally:
                cleanup_task = asyncio.create_task(
                    self._finalize_experiment_runtime(
                        session_id=session_id,
                        experiment_dir=experiment_dir,
                        payload=payload,
                        capture_id=capture_id,
                        stream_start_status=stream_start_status,
                        capture_transport_generation=capture_transport_generation,
                        trace_started=trace_started,
                        execution_deadline=deadline,
                        canceled=cancelled_error is not None,
                    ),
                    name=f"finalize-{experiment_id}",
                )
                try:
                    cleanup_result = await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    cleanup_result = await cleanup_task
                stop_payload = dict(cleanup_result.get("stop_payload") or {})
                cleanup_status = dict(cleanup_result.get("final_status_payload") or {})
                if cleanup_status:
                    final_status_payload = cleanup_status
                trace_paths = list(cleanup_result.get("trace_paths") or [])
                screenshot_paths.extend(
                    str(item) for item in cleanup_result.get("screenshot_paths", [])
                )
                snapshot_paths.extend(
                    str(item) for item in cleanup_result.get("snapshot_paths", [])
                )
                network_payload = dict(cleanup_result.get("network_payload") or {})
                collector_stopped = bool(cleanup_result.get("collector_stopped"))
                warnings.extend(str(item) for item in cleanup_result.get("warnings", []))
                errors.extend(str(item) for item in cleanup_result.get("errors", []))

            post_alignment = AlignmentResult(
                status=(
                    "not_checked_due_to_cancel" if cancelled_error is not None else "not_checked"
                ),
                playwright_page=alignment.playwright_page,
                warnings=[
                    (
                        "Post-flow page alignment was not checked because the experiment "
                        "was canceled."
                        if cancelled_error is not None
                        else "Post-flow page alignment was not checked."
                    )
                ],
            )
            if cancelled_error is None:
                try:
                    post_deadline = Deadline(2_500)
                    post_page = await self.playwright.current_page(session_id, post_deadline)
                    post_alignment = await self.js_reverse.align_page(
                        post_page,
                        post_deadline,
                        page_id=(
                            str(session["js_reverse_page_id"])
                            if session.get("js_reverse_page_id")
                            else None
                        ),
                    )
                except Exception as exc:
                    warnings.append(f"post-flow alignment: {str(exc)[:3500]}")

            evidence_collection = await self._collect_post_flow_evidence(
                experiment_id=experiment_id,
                experiment_dir=experiment_dir,
                manifest=manifest,
                payload=payload,
                network_payload=network_payload,
                network_checkpoint_value=network_checkpoint_value,
                request_matcher=request_matcher,
                canceled=cancelled_error is not None,
                step_results=step_results,
                console_checkpoint_value=console_checkpoint_value,
                warnings=warnings,
            )
            network_payload = evidence_collection.network_payload
            primary_network_payload = evidence_collection.primary_network_payload
            evidence_entries = evidence_collection.evidence_entries
            evidence_artifacts = evidence_collection.artifacts
            replay_analysis = self._analyze_replay_evidence_stage(
                replay_plan=replay_plan,
                manifest=manifest,
                evidence_entries=evidence_entries,
                final_status_payload=final_status_payload,
                replay_response=replay_response,
                replay_http_status=replay_http_status,
                replay_response_content_type=replay_response_content_type,
                replay_observed_response_mode=replay_observed_response_mode,
                pre_dispatch_alignment=pre_dispatch_alignment,
                post_response_alignment=post_response_alignment,
                post_alignment=post_alignment,
                warnings=warnings,
                errors=errors,
            )
            replay_http_status = replay_analysis.http_status
            replay_response_content_type = replay_analysis.response_content_type
            replay_network_evidence_id = replay_analysis.network_evidence_id
            wire_snapshot = replay_analysis.wire_snapshot
            mutation_assessment = replay_analysis.mutation_assessment
            response_analysis = replay_analysis.response_analysis
            stream_response_contract = replay_analysis.stream_response_contract
            response_evidence_source = replay_analysis.response_evidence_source
            pre_dispatch_environment = replay_analysis.pre_dispatch_environment
            post_response_environment = replay_analysis.post_response_environment
            post_verification_environment = replay_analysis.post_verification_environment
            observation_assembly = self._assemble_observations_stage(
                payload=payload,
                replay_plan=replay_plan,
                experiment_id=experiment_id,
                evidence_entries=evidence_entries,
                final_status_payload=final_status_payload,
                primary_network_payload=primary_network_payload,
                replay_network_evidence_id=replay_network_evidence_id,
                replay_observed_response_mode=replay_observed_response_mode,
                stream_response_contract=stream_response_contract,
                response_evidence_source=response_evidence_source,
                step_results=step_results,
                alignment=alignment,
                post_alignment=post_alignment,
                wait_observations=wait_observations,
                wire_snapshot=wire_snapshot,
                replay_http_status=replay_http_status,
                replay_response_content_type=replay_response_content_type,
                pre_dispatch_environment=pre_dispatch_environment,
                errors=errors,
            )
            primary_requests = observation_assembly.primary_requests
            count_ok = observation_assembly.count_satisfied
            cancellation_classifications = (
                observation_assembly.cancellation_classifications
            )
            network_observations = observation_assembly.network_observations
            comparison_results = observation_assembly.comparison_results
            extractor_observations = observation_assembly.extractor_observations
            observed_stream_response = observation_assembly.observed_stream_response
            non_stream_error_response_observed = (
                observation_assembly.non_stream_error_response_observed
            )
            stream_evidence_required = observation_assembly.stream_evidence_required
            return await self._complete_capture_record(
                CaptureCompletionContext(
                    operation=request.operation,
                    session_id=session_id,
                    experiment_id=experiment_id,
                    manifest=manifest,
                    payload=payload,
                    deadline=deadline,
                    alignment=alignment,
                    post_alignment=post_alignment,
                    step_results=step_results,
                    wait_result=wait_result,
                    wait_observations=wait_observations,
                    cancelled_error=cancelled_error,
                    errors=errors,
                    warnings=warnings,
                    replay_plan=replay_plan,
                    replay_result=replay_result,
                    replay_http_status=replay_http_status,
                    replay_response_content_type=replay_response_content_type,
                    mutation_assessment=mutation_assessment,
                    response_analysis=response_analysis,
                    stream_response_contract=stream_response_contract,
                    response_evidence_source=response_evidence_source,
                    replay_network_evidence_id=replay_network_evidence_id,
                    pre_dispatch_environment=pre_dispatch_environment,
                    post_response_environment=post_response_environment,
                    post_verification_environment=post_verification_environment,
                    comparison_results=comparison_results,
                    extractor_observations=extractor_observations,
                    network_observations=network_observations,
                    cancellation_classifications=cancellation_classifications,
                    primary_requests=primary_requests,
                    count_satisfied=count_ok,
                    observed_stream_response=observed_stream_response,
                    non_stream_error_response_observed=(
                        non_stream_error_response_observed
                    ),
                    stream_evidence_required=stream_evidence_required,
                    collector_started=collector_started,
                    collector_stopped=collector_stopped,
                    collector_start_wall_time_ms=collector_start_wall_time_ms,
                    first_mutation_wall_time_ms=first_mutation_wall_time_ms,
                    cleanup_result=cleanup_result,
                    capture_id=capture_id,
                    capture_uuid=capture_uuid,
                    capture_relative_dir=capture_relative_dir,
                    capture_metadata_artifact_id=capture_metadata_artifact_id,
                    capture_transport_generation=capture_transport_generation,
                    stream_start_status=stream_start_status,
                    start_payload=start_payload,
                    final_status_payload=final_status_payload,
                    stop_payload=stop_payload,
                    network_payload=network_payload,
                    network_checkpoint_value=network_checkpoint_value,
                    console_checkpoint_value=console_checkpoint_value,
                    evidence_entries=evidence_entries,
                    evidence_artifacts=evidence_artifacts,
                    replay_artifacts=replay_artifacts,
                    screenshot_paths=screenshot_paths,
                    snapshot_paths=snapshot_paths,
                    trace_paths=trace_paths,
                )
            )
