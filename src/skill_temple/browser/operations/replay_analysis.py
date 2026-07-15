"""Replay evidence audit, response analysis, and environment facts."""

from __future__ import annotations

from typing import Any

from ...protocol.analyzers.response import analyze_replay_response
from ...protocol.fingerprints import request_body_canonical_sha256_from_snapshot
from ...protocol.mutations import (
    assess_mutation_effectiveness,
    observe_binding_application,
    replay_operation_overwritten_by_later,
)
from ...protocol_evidence import response_content_type, response_value_from_snapshot
from ..adapters.contracts import AlignmentResult
from .context import ReplayAnalysisResult


class BrowserReplayAnalysisOperations:
    """Analyze replay facts after evidence export without owning capture execution."""

    def _analyze_replay_evidence_stage(
        self,
        *,
        replay_plan: dict[str, Any] | None,
        manifest: dict[str, Any],
        evidence_entries: list[dict[str, Any]],
        final_status_payload: dict[str, Any],
        replay_response: Any,
        replay_http_status: int | None,
        replay_response_content_type: str | None,
        replay_observed_response_mode: str | None,
        pre_dispatch_alignment: AlignmentResult,
        post_response_alignment: AlignmentResult | None,
        post_alignment: AlignmentResult,
        warnings: list[str],
        errors: list[str],
    ) -> ReplayAnalysisResult:
        mutation_assessment: dict[str, Any] | None = None
        response_analysis: dict[str, Any] | None = None
        stream_response_contract: dict[str, Any] | None = None
        response_evidence_source: str | None = None
        replay_network_evidence_id: str | None = None
        wire_snapshot: dict[str, Any] | None = None
        pre_dispatch_environment: dict[str, Any] | None = None
        post_response_environment: dict[str, Any] | None = None
        post_verification_environment: dict[str, Any] | None = None
        if replay_plan is not None:
            replay_network_entry, replay_selection_error = self._select_replay_network_evidence(
                evidence_entries,
                replay_plan,
            )
            if replay_selection_error:
                errors.append(replay_selection_error)
            replay_network_evidence_id = (
                str(replay_network_entry.get("evidence_id"))
                if isinstance(replay_network_entry, dict)
                and replay_network_entry.get("evidence_id")
                else None
            )
            wire_snapshot = self._network_evidence_snapshot(
                self.experiments.root,
                replay_network_entry,
            )
            associated_replay_streams: list[dict[str, Any]] = []
            if replay_network_evidence_id:
                for stream_request in final_status_payload.get("requests", []):
                    if not isinstance(stream_request, dict):
                        continue
                    exact_evidence, _ = self._associate_stream_network_evidence(
                        stream_request,
                        [
                            item
                            for item in evidence_entries
                            if item.get("kind") == "network_request"
                        ],
                    )
                    if (
                        isinstance(exact_evidence, dict)
                        and exact_evidence.get("evidence_id") == replay_network_evidence_id
                    ):
                        associated_replay_streams.append(stream_request)
            if len(associated_replay_streams) == 1:
                self._mark_snapshot_headers_complete_from_stream(
                    wire_snapshot,
                    associated_replay_streams[0],
                )
            if isinstance(wire_snapshot, dict):
                exact_status = wire_snapshot.get("status")
                if isinstance(exact_status, int):
                    replay_http_status = exact_status
                exact_content_type = response_content_type(wire_snapshot)
                if exact_content_type:
                    replay_response_content_type = exact_content_type
            replay_plan["network_evidence_id"] = replay_network_evidence_id
            replay_manifest = manifest.get("replay")
            if isinstance(replay_manifest, dict):
                replay_manifest["network_evidence_id"] = replay_network_evidence_id
            replay_mutations = list(replay_plan.get("mutations", []))
            mutation_observations = [
                assess_mutation_effectiveness(
                    item,
                    wire_snapshot,
                    overwritten_by_later=any(
                        replay_operation_overwritten_by_later(item, later)
                        for later in replay_mutations[index + 1 :]
                    ),
                )
                for index, item in enumerate(replay_mutations)
            ]
            resolved_binding_specs = [
                item
                for item in replay_plan["_binding_specs"]
                if item.binding_id in replay_plan["binding_values"]
            ]
            binding_observation = observe_binding_application(
                wire_snapshot,
                bindings=resolved_binding_specs,
                binding_values=replay_plan["binding_values"],
                mutations=replay_mutations,
            )
            mutation_assessment = {
                "mutations": mutation_observations,
                "all_mutations_effective": (
                    all(
                        item.get("mutation_effective") is True
                        or item.get("final_wire_observability")
                        == "overwritten_by_later_operation"
                        for item in mutation_observations
                    )
                    if mutation_observations
                    else True
                ),
                "all_mutations_applied_to_spec": all(
                    item.get("operation_applied_to_spec") is True
                    for item in mutation_observations
                ),
                "bindings": binding_observation,
                "unresolved_binding_ids": replay_plan.get("unresolved_binding_ids", []),
            }
            exact_response_value = response_value_from_snapshot(wire_snapshot)
            exact_replay_response_value = self._complete_replay_response_value(replay_response)
            response_value = (
                exact_response_value
                if exact_response_value is not None
                else exact_replay_response_value
                if exact_replay_response_value is not None
                else replay_response
            )
            response_evidence_source = (
                "exact_network_response_body"
                if exact_response_value is not None
                else "complete_replay_response_body"
                if exact_replay_response_value is not None
                else "replay_preview_fallback"
            )
            response_analyzer = replay_plan.get("response_analyzer")
            if isinstance(response_analyzer, dict):
                response_analysis = analyze_replay_response(
                    status=replay_http_status,
                    content_type=replay_response_content_type,
                    response_value=response_value,
                    mutation=(
                        replay_plan["mutations"][0]
                        if len(replay_plan.get("mutations", [])) == 1
                        else None
                    ),
                    redirected=bool(
                        self._extract_response_field(replay_response, "redirected")
                    ),
                    final_url=(
                        str(value)
                        if (
                            value := self._extract_response_field(
                                replay_response,
                                "url",
                            )
                        )
                        else None
                    ),
                    source_url=str(replay_plan["spec"].get("url", "")),
                    source_content_type=replay_plan.get("source_content_type"),
                )
                response_analysis["evidence_source"] = response_evidence_source
                response_analysis["evidence_sufficient"] = response_evidence_source in {
                    "exact_network_response_body",
                    "complete_replay_response_body",
                }
                observations = response_analysis.get("observations")
                if isinstance(observations, dict) and isinstance(
                    mutation_assessment,
                    dict,
                ):
                    observations["mutation_effective"] = mutation_assessment.get(
                        "all_mutations_effective"
                    )
            self._apply_observed_replay_mode(
                replay_plan,
                replay_observed_response_mode,
            )
            replay_manifest = manifest.get("replay")
            if isinstance(replay_manifest, dict):
                replay_manifest["replay_protocol"] = replay_plan[
                    "replay_protocol"
                ]
                replay_manifest["replay_protocol_hash"] = replay_plan[
                    "replay_protocol_hash"
                ]
                replay_manifest["observed_response_mode"] = replay_plan.get(
                    "observed_response_mode"
                )
                replay_manifest["response_is_stream"] = replay_plan.get(
                    "response_is_stream"
                )
            stream_response_contract = self._stream_response_contract(
                replay_plan,
                replay_response,
                status=replay_http_status,
                content_type=replay_response_content_type,
            )
            if (
                isinstance(stream_response_contract, dict)
                and stream_response_contract.get("status") == "partial"
            ):
                warnings.append(
                    "Streaming response did not satisfy the configured terminal contract."
                )
            environment_policy = replay_plan.get("environment_comparison")
            environment_policy = (
                environment_policy if isinstance(environment_policy, dict) else {}
            )
            context_header_names = environment_policy.get("context_header_names")
            context_header_names = (
                [str(item) for item in context_header_names]
                if isinstance(context_header_names, list)
                else None
            )
            pre_dispatch_environment = self._environment_fingerprint(
                pre_dispatch_alignment,
                wire_snapshot,
                phase="pre_dispatch",
                ignored_cookie_names=replay_plan.get("ignored_cookie_names"),
                ignored_context_headers=replay_plan.get("ignored_context_headers"),
                context_header_names=context_header_names,
            )
            post_response_environment = self._environment_fingerprint(
                post_response_alignment,
                None,
                phase="post_response",
                include_request_context=False,
                ignored_cookie_names=replay_plan.get("ignored_cookie_names"),
                ignored_context_headers=replay_plan.get("ignored_context_headers"),
                context_header_names=context_header_names,
            )
            post_verification_environment = self._environment_fingerprint(
                post_alignment,
                None,
                phase="post_verification",
                include_request_context=False,
                ignored_cookie_names=replay_plan.get("ignored_cookie_names"),
                ignored_context_headers=replay_plan.get("ignored_context_headers"),
                context_header_names=context_header_names,
            )
        return ReplayAnalysisResult(
            http_status=replay_http_status,
            response_content_type=replay_response_content_type,
            network_evidence_id=replay_network_evidence_id,
            wire_snapshot=wire_snapshot,
            mutation_assessment=mutation_assessment,
            response_analysis=response_analysis,
            stream_response_contract=stream_response_contract,
            response_evidence_source=response_evidence_source,
            pre_dispatch_environment=pre_dispatch_environment,
            post_response_environment=post_response_environment,
            post_verification_environment=post_verification_environment,
        )

    def _build_replay_comparisons_stage(
        self,
        *,
        replay_plan: dict[str, Any] | None,
        network_observations: list[dict[str, Any]],
        replay_network_evidence_id: str | None,
        wire_snapshot: dict[str, Any] | None,
        replay_http_status: int | None,
        replay_response_content_type: str | None,
        pre_dispatch_environment: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if replay_plan is None:
            return []
        current_stream_facts, current_stream_status = (
            self._current_replay_stream_summary(
                network_observations,
                replay_network_evidence_id,
            )
        )
        return self._build_replay_comparison_results(
            replay_plan,
            current_request_body_sha256=(
                request_body_canonical_sha256_from_snapshot(wire_snapshot)
                if isinstance(wire_snapshot, dict)
                else None
            ),
            current_response_status=replay_http_status,
            current_response_content_type=replay_response_content_type,
            current_stream_facts=current_stream_facts,
            current_environment=pre_dispatch_environment,
            current_status_overrides=(
                {"stream_summary": current_stream_status}
                if current_stream_status
                else None
            ),
        )
