"""Small typed state transfers between browser operation stages."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ...browser_models import CaptureFlowPayload, FlowStepResult
from ..adapters import AlignmentResult, StreamCheckpoint


@dataclass(slots=True)
class ReplayDispatchResult:
    stream_checkpoint: StreamCheckpoint
    first_mutation_wall_time_ms: int | None
    replay_result: dict[str, Any] = field(default_factory=dict)
    replay_response: Any = None
    http_status: int | None = None
    response_content_type: str | None = None
    observed_response_mode: str | None = None
    post_response_alignment: AlignmentResult | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ReplayPreparationResult:
    stream_checkpoint: StreamCheckpoint
    first_mutation_wall_time_ms: int | None
    pre_dispatch_alignment: AlignmentResult
    artifacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class EvidenceCollectionResult:
    network_payload: dict[str, Any]
    primary_network_payload: dict[str, Any]
    evidence_entries: list[dict[str, Any]]
    artifacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ReplayAnalysisResult:
    http_status: int | None = None
    response_content_type: str | None = None
    network_evidence_id: str | None = None
    wire_snapshot: dict[str, Any] | None = None
    mutation_assessment: dict[str, Any] | None = None
    response_analysis: dict[str, Any] | None = None
    stream_response_contract: dict[str, Any] | None = None
    response_evidence_source: str | None = None
    pre_dispatch_environment: dict[str, Any] | None = None
    post_response_environment: dict[str, Any] | None = None
    post_verification_environment: dict[str, Any] | None = None


@dataclass(slots=True)
class ObservationAssemblyResult:
    primary_requests: list[dict[str, Any]]
    count_satisfied: bool
    cancellation_classifications: list[dict[str, Any]]
    network_observations: list[dict[str, Any]]
    comparison_results: list[dict[str, Any]]
    extractor_observations: list[dict[str, Any]]
    observed_stream_response: bool
    non_stream_error_response_observed: bool
    stream_evidence_required: bool


@dataclass(slots=True)
class CaptureCompletionContext:
    operation: str
    session_id: str
    experiment_id: str
    manifest: dict[str, Any]
    payload: CaptureFlowPayload
    deadline: Any
    alignment: AlignmentResult
    post_alignment: AlignmentResult
    step_results: list[FlowStepResult]
    wait_result: dict[str, Any] | None
    wait_observations: list[dict[str, Any]]
    cancelled_error: asyncio.CancelledError | None
    errors: list[str]
    warnings: list[str]
    replay_plan: dict[str, Any] | None
    replay_result: dict[str, Any]
    replay_http_status: int | None
    replay_response_content_type: str | None
    mutation_assessment: dict[str, Any] | None
    response_analysis: dict[str, Any] | None
    stream_response_contract: dict[str, Any] | None
    response_evidence_source: str | None
    replay_network_evidence_id: str | None
    pre_dispatch_environment: dict[str, Any] | None
    post_response_environment: dict[str, Any] | None
    post_verification_environment: dict[str, Any] | None
    comparison_results: list[dict[str, Any]]
    extractor_observations: list[dict[str, Any]]
    network_observations: list[dict[str, Any]]
    cancellation_classifications: list[dict[str, Any]]
    primary_requests: list[dict[str, Any]]
    count_satisfied: bool
    observed_stream_response: bool
    non_stream_error_response_observed: bool
    stream_evidence_required: bool
    collector_started: bool
    collector_stopped: bool
    collector_start_wall_time_ms: int | None
    first_mutation_wall_time_ms: int | None
    cleanup_result: dict[str, Any]
    capture_id: int | None
    capture_uuid: str | None
    capture_relative_dir: str | None
    capture_metadata_artifact_id: str | None
    capture_transport_generation: int | None
    stream_start_status: str
    start_payload: dict[str, Any]
    final_status_payload: dict[str, Any]
    stop_payload: dict[str, Any]
    network_payload: dict[str, Any]
    network_checkpoint_value: dict[str, Any]
    console_checkpoint_value: dict[str, Any]
    evidence_entries: list[dict[str, Any]]
    evidence_artifacts: list[dict[str, Any]]
    replay_artifacts: list[dict[str, Any]]
    screenshot_paths: list[str]
    snapshot_paths: list[str]
    trace_paths: list[str]
