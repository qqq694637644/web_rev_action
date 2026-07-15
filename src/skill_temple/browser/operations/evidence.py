"""Evidence collection and factual comparison with explicit dependencies."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ...browser_models import CaptureFlowPayload, FlowStepResult, RequestMatcher
from ...protocol.analyzers.differences import (
    aggregate_dimension_status,
    compare_dimension,
    compare_environment_facts,
    select_current_stream_summary,
    stream_summary_from_observation,
)
from ...protocol.fingerprints import request_body_canonical_sha256_from_snapshot
from ...protocol.matching import (
    network_request_matches,
    requests_after_checkpoint,
    select_network_evidence,
)
from ...protocol.shapes import (
    redacted_request_body_from_snapshot,
    request_shape_from_snapshot,
)
from ...protocol_evidence import (
    build_network_observation,
    evidence_id,
    load_snapshot,
    network_snapshot_dimensions,
    public_network_summary,
    response_content_type,
    stream_request_has_complete_request_headers,
)
from ..adapters.contracts import AlignmentResult
from ..core import BrowserServiceError, Deadline
from .context import EvidenceCollectionResult, ObservationAssemblyResult


class BrowserEvidenceOperations:
    """Own evidence behavior while the public service remains a facade."""

    async def _export_network_evidence(
        self,
        *,
        experiment_id: str,
        experiment_dir: Path,
        selectors: list[Any],
        requests: list[dict[str, Any]],
        deadline: Deadline,
        step_ids: list[str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        evidence_entries: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        warnings: list[str] = []
        network_root = experiment_dir / "js-reverse" / "network"
        network_root.mkdir(parents=True, exist_ok=True)
        for selector in selectors:
            matches = select_network_evidence(requests, selector)
            for ordinal, request in enumerate(matches, start=1):
                reqid = request.get("reqid")
                if not isinstance(reqid, int):
                    continue
                ev_id = evidence_id(
                    experiment_id,
                    "network_request",
                    selector_id=selector.selector_id,
                    stable_id=reqid,
                    ordinal=ordinal,
                )
                target_dir = network_root / ev_id
                target_dir.mkdir(parents=True, exist_ok=True)
                artifact_ids: list[str] = []
                artifact_paths: dict[str, str] = {}
                snapshot: dict[str, Any] | None = None
                for part in list(dict.fromkeys(selector.export_parts)):
                    suffix = ".bin" if part in {"requestBody", "responseBody"} else ".json"
                    output_file = target_dir / f"{part}{suffix}"
                    try:
                        await self.js_reverse.export_network_request(
                            reqid,
                            output_file,
                            part,
                            deadline.child(min(5_000, deadline.remaining_ms())),
                        )
                    except Exception as exc:
                        warnings.append(
                            f"network evidence {selector.selector_id} reqid={reqid} "
                            f"part={part}: {str(exc)[:2000]}"
                        )
                        continue
                    artifact_id = f"art_{ev_id}_{part}"
                    descriptor = self.experiments.describe_local_artifact(
                        str(output_file),
                        artifact_id=artifact_id,
                        kind=f"network_{part}",
                        sensitivity=(
                            "credential"
                            if part
                            in {
                                "all",
                                "requestBody",
                                "responseBody",
                                "responseHeaders",
                            }
                            else "private"
                        ),
                        contains_credentials=part
                        in {
                            "all",
                            "requestBody",
                            "responseBody",
                            "responseHeaders",
                        },
                    )
                    if descriptor:
                        artifacts.append(descriptor)
                        artifact_ids.append(artifact_id)
                        artifact_paths[part] = str(descriptor["relativePath"])
                    if part == "all" and output_file.is_file():
                        try:
                            snapshot = load_snapshot(output_file)
                        except (OSError, ValueError, json.JSONDecodeError) as exc:
                            warnings.append(
                                f"network evidence snapshot reqid={reqid}: {str(exc)[:1000]}"
                            )
                if snapshot is not None:
                    shape = request_shape_from_snapshot(snapshot)
                    redacted_body = redacted_request_body_from_snapshot(snapshot)
                    if shape is not None:
                        shape_file = target_dir / "request-shape.json"
                        shape_file.write_text(
                            json.dumps(shape, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        shape_artifact_id = f"art_{ev_id}_request_shape"
                        descriptor = self.experiments.describe_local_artifact(
                            str(shape_file),
                            artifact_id=shape_artifact_id,
                            kind="request_shape",
                            sensitivity="public",
                        )
                        if descriptor:
                            artifacts.append(descriptor)
                            artifact_ids.append(shape_artifact_id)
                            artifact_paths["request_shape"] = str(descriptor["relativePath"])
                    if redacted_body is not None:
                        redacted_file = target_dir / "request-body.redacted.json"
                        redacted_file.write_text(
                            json.dumps(redacted_body, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        redacted_artifact_id = f"art_{ev_id}_request_body_redacted"
                        descriptor = self.experiments.describe_local_artifact(
                            str(redacted_file),
                            artifact_id=redacted_artifact_id,
                            kind="request_body_redacted",
                            sensitivity="public",
                        )
                        if descriptor:
                            artifacts.append(descriptor)
                            artifact_ids.append(redacted_artifact_id)
                            artifact_paths["request_body_redacted"] = str(
                                descriptor["relativePath"]
                            )
                if selector.include_initiator:
                    try:
                        initiator = await self.js_reverse.get_request_initiator(
                            reqid,
                            deadline.child(min(3_000, deadline.remaining_ms())),
                        )
                        initiator_file = target_dir / "initiator.json"
                        initiator_file.write_text(
                            json.dumps(initiator, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        initiator_artifact_id = f"art_{ev_id}_initiator"
                        descriptor = self.experiments.describe_local_artifact(
                            str(initiator_file),
                            artifact_id=initiator_artifact_id,
                            kind="request_initiator",
                            sensitivity="private",
                        )
                        if descriptor:
                            artifacts.append(descriptor)
                            artifact_ids.append(initiator_artifact_id)
                            artifact_paths["initiator"] = str(descriptor["relativePath"])
                    except Exception as exc:
                        warnings.append(f"request initiator reqid={reqid}: {str(exc)[:2000]}")
                cookie_artifacts: list[str] = []
                if selector.include_cookie_provenance:
                    for cookie_name in selector.cookie_names:
                        try:
                            cookie_flow = await self.js_reverse.trace_cookie_provenance(
                                cookie_name,
                                deadline.child(min(3_000, deadline.remaining_ms())),
                            )
                            safe_cookie = re.sub(r"[^A-Za-z0-9_.-]+", "-", cookie_name)
                            cookie_file = target_dir / f"cookie-{safe_cookie}.json"
                            cookie_file.write_text(
                                json.dumps(cookie_flow, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8",
                            )
                            cookie_artifact_id = f"art_{ev_id}_cookie_{safe_cookie}"
                            descriptor = self.experiments.describe_local_artifact(
                                str(cookie_file),
                                artifact_id=cookie_artifact_id,
                                kind="cookie_provenance",
                                sensitivity="credential",
                                contains_credentials=True,
                            )
                            if descriptor:
                                artifacts.append(descriptor)
                                artifact_ids.append(cookie_artifact_id)
                                cookie_artifacts.append(cookie_artifact_id)
                        except Exception as exc:
                            warnings.append(f"cookie provenance {cookie_name}: {str(exc)[:2000]}")
                evidence_entries.append(
                    {
                        "evidence_id": ev_id,
                        "kind": "network_request",
                        "selector_id": selector.selector_id,
                        "request_ids": {
                            "reqid": reqid,
                            "collector_generation": (
                                snapshot.get("collectorGeneration")
                                if isinstance(snapshot, dict)
                                and snapshot.get("collectorGeneration") is not None
                                else request.get("collectorGeneration")
                                if request.get("collectorGeneration") is not None
                                else self._transport_generation()
                            ),
                            "network_request_id": (
                                snapshot.get("networkRequestId")
                                if isinstance(snapshot, dict)
                                else request.get("networkRequestId")
                            ),
                            "cdp_request_id": (
                                snapshot.get("cdpRequestId")
                                if isinstance(snapshot, dict)
                                else request.get("cdpRequestId")
                            ),
                            "persistent_request_id": (
                                snapshot.get("persistentRequestId")
                                if isinstance(snapshot, dict)
                                else request.get("persistentRequestId")
                            ),
                        },
                        "request_body_canonical_sha256": (
                            request_body_canonical_sha256_from_snapshot(snapshot)
                            if isinstance(snapshot, dict)
                            else None
                        ),
                        "observed_at": (
                            snapshot.get("observedAt")
                            if isinstance(snapshot, dict)
                            else request.get("observedAt")
                        ),
                        "artifact_ids": artifact_ids,
                        "artifact_paths": artifact_paths,
                        "cookie_artifact_ids": cookie_artifacts,
                        "step_ids": step_ids,
                        "summary": (
                            public_network_summary(snapshot)
                            if snapshot is not None
                            else {
                                "url": str(request.get("url", ""))[:8192],
                                "method": request.get("method"),
                                "resource_type": request.get("resourceType"),
                                "status": request.get("status"),
                            }
                        ),
                    }
                )
        return evidence_entries, artifacts, warnings

    async def _console_checkpoint(self, deadline: Deadline) -> dict[str, Any]:
        payload = await self.js_reverse.list_console_messages(
            deadline,
            types=["error", "warn"],
        )
        messages = payload.get("messages")
        values = (
            [item for item in messages if isinstance(item, dict)]
            if isinstance(messages, list)
            else []
        )
        ids = [int(item["msgid"]) for item in values if isinstance(item.get("msgid"), int)]
        return {"max_msgid": max(ids, default=0)}

    async def _export_console_evidence(
        self,
        *,
        experiment_id: str,
        experiment_dir: Path,
        checkpoint: dict[str, Any],
        deadline: Deadline,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
        try:
            payload = await self.js_reverse.list_console_messages(
                deadline,
                types=["error", "warn"],
            )
        except Exception as exc:
            return [], [], [f"console evidence: {str(exc)[:2000]}"]
        messages = payload.get("messages")
        values = (
            [item for item in messages if isinstance(item, dict)]
            if isinstance(messages, list)
            else []
        )
        max_msgid = int(checkpoint.get("max_msgid", 0) or 0)
        selected = [
            item
            for item in values
            if isinstance(item.get("msgid"), int) and int(item["msgid"]) > max_msgid
        ]
        if not selected:
            return [], [], []
        console_dir = experiment_dir / "js-reverse" / "console"
        console_dir.mkdir(parents=True, exist_ok=True)
        console_file = console_dir / "console.jsonl"
        console_file.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in selected),
            encoding="utf-8",
        )
        artifact_id = f"art_{experiment_id}_console_errors"
        descriptor = self.experiments.describe_local_artifact(
            str(console_file),
            artifact_id=artifact_id,
            kind="console_errors",
            sensitivity="private",
        )
        artifacts = [descriptor] if descriptor else []
        evidence_entries = [
            {
                "evidence_id": evidence_id(
                    experiment_id,
                    "console_message",
                    stable_id=item.get("msgid"),
                    ordinal=index,
                ),
                "kind": "console_message",
                "message_id": item.get("msgid"),
                "artifact_ids": [artifact_id] if descriptor else [],
                "artifact_paths": {"console": descriptor["relativePath"] if descriptor else None},
                "message_index": index - 1,
                "summary": {
                    key: item.get(key)
                    for key in (
                        "type",
                        "text",
                        "url",
                        "lineNumber",
                        "columnNumber",
                        "timestamp",
                    )
                    if key in item
                },
            }
            for index, item in enumerate(selected, start=1)
        ]
        return evidence_entries, artifacts, []

    @staticmethod
    def _network_evidence_snapshot(
        root: Path,
        entry: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        paths = entry.get("artifact_paths")
        paths = paths if isinstance(paths, dict) else {}
        relative = paths.get("all")
        if not isinstance(relative, str):
            return None
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
            return load_snapshot(path)
        except (ValueError, OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _select_replay_network_evidence(
        entries: list[dict[str, Any]],
        replay_plan: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str | None]:
        expected_hash = replay_plan.get("expected_request_body_canonical_sha256")
        expected_spec = replay_plan.get("spec")
        expected_spec = expected_spec if isinstance(expected_spec, dict) else {}
        expected_url = str(expected_spec.get("url") or "")
        expected_method = str(expected_spec.get("method") or "GET").upper()
        dispatch_wall_time_ms = replay_plan.get("dispatch_wall_time_ms")
        window_end_wall_time_ms = replay_plan.get("correlation_window_end_wall_time_ms")
        if not isinstance(dispatch_wall_time_ms, int) or not isinstance(
            window_end_wall_time_ms, int
        ):
            return None, "Replay correlation window is missing."
        candidates = [
            item
            for item in entries
            if item.get("kind") == "network_request"
            and item.get("selector_id") == "replay_request"
            and isinstance(item.get("summary"), dict)
            and str(item["summary"].get("url") or "") == expected_url
            and str(item["summary"].get("method") or "").upper() == expected_method
            and (
                expected_hash is None or item.get("request_body_canonical_sha256") == expected_hash
            )
            and (
                isinstance(item.get("observed_at"), (int, float))
                and dispatch_wall_time_ms - 1_000
                <= int(item["observed_at"])
                <= window_end_wall_time_ms
            )
        ]
        if len(candidates) == 1:
            return candidates[0], None
        if not candidates:
            return (
                None,
                "No replay request matched the expected method, full URL, canonical body "
                "fingerprint, and dispatch window.",
            )
        return (
            None,
            "Multiple replay requests matched the method, full URL, canonical body "
            "fingerprint, and dispatch window; the replay request is ambiguous.",
        )

    @staticmethod
    def _associate_stream_network_evidence(
        request: dict[str, Any],
        network_entries: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        def request_ids(item: dict[str, Any]) -> dict[str, Any]:
            value = item.get("request_ids")
            return value if isinstance(value, dict) else {}

        stable_constraints: list[tuple[str, list[dict[str, Any]]]] = []
        network_request_id = request.get("networkRequestId")
        collector_generation = request.get("collectorGeneration")
        if network_request_id is not None:
            stable_constraints.append(
                (
                    "network_request_id",
                    [
                        item
                        for item in network_entries
                        if request_ids(item).get("network_request_id") == network_request_id
                        and (
                            collector_generation is None
                            or request_ids(item).get("collector_generation") == collector_generation
                        )
                    ],
                )
            )
        cdp_request_id = request.get("cdpRequestId")
        if cdp_request_id is not None:
            stable_constraints.append(
                (
                    "cdp_request_id",
                    [
                        item
                        for item in network_entries
                        if request_ids(item).get("cdp_request_id") == cdp_request_id
                    ],
                )
            )
        persistent_request_id = request.get("persistentRequestId")
        if persistent_request_id is not None:
            stable_constraints.append(
                (
                    "persistent_request_id",
                    [
                        item
                        for item in network_entries
                        if request_ids(item).get("persistent_request_id") == persistent_request_id
                    ],
                )
            )
        usable_constraints = [
            (method, candidates) for method, candidates in stable_constraints if candidates
        ]
        if usable_constraints:
            candidate_ids = {id(item) for item in usable_constraints[0][1]}
            for _, candidates in usable_constraints[1:]:
                candidate_ids.intersection_update(id(item) for item in candidates)
            candidates = [item for item in network_entries if id(item) in candidate_ids]
            method = "+".join(item[0] for item in usable_constraints)
            if len(candidates) == 1:
                return candidates[0], {
                    "status": "matched",
                    "method": method,
                    "candidate_count": 1,
                    "collector_generation_constrained": collector_generation is not None,
                }
            if len(candidates) > 1:
                fallback_candidates = [
                    item
                    for item in candidates
                    if isinstance(item.get("summary"), dict)
                    and item["summary"].get("url") == request.get("url")
                    and item["summary"].get("method") == request.get("method")
                ]
                if len(fallback_candidates) == 1:
                    return fallback_candidates[0], {
                        "status": "matched",
                        "method": f"{method}+url_method_fallback",
                        "candidate_count": 1,
                        "collector_generation_constrained": (collector_generation is not None),
                    }
                return None, {
                    "status": "ambiguous",
                    "method": method,
                    "candidate_count": len(candidates),
                    "collector_generation_constrained": collector_generation is not None,
                }
            return None, {
                "status": "ambiguous",
                "method": method,
                "candidate_count": sum(len(item[1]) for item in usable_constraints),
                "collector_generation_constrained": collector_generation is not None,
            }
        fallback_candidates = [
            item
            for item in network_entries
            if isinstance(item.get("summary"), dict)
            and item["summary"].get("url") == request.get("url")
            and item["summary"].get("method") == request.get("method")
        ]
        if len(fallback_candidates) == 1:
            return fallback_candidates[0], {
                "status": "matched",
                "method": "url_method_fallback",
                "candidate_count": 1,
            }
        if len(fallback_candidates) > 1:
            return None, {
                "status": "ambiguous",
                "method": "url_method_fallback",
                "candidate_count": len(fallback_candidates),
            }
        return None, {
            "status": "not_found",
            "method": None,
            "candidate_count": 0,
        }

    @classmethod
    def _mark_snapshot_headers_complete_from_stream(
        cls,
        snapshot: dict[str, Any] | None,
        stream_request: dict[str, Any] | None,
    ) -> None:
        if not isinstance(snapshot, dict) or not isinstance(stream_request, dict):
            return
        if stream_request_has_complete_request_headers(stream_request):
            snapshot["requestHeadersCompleteness"] = "complete"

    @classmethod
    def _build_network_observations(
        cls,
        experiment_id: str,
        primary_requests: list[dict[str, Any]],
        network_entries: list[dict[str, Any]],
        *,
        stream_capture: bool,
    ) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        for ordinal, request in enumerate(primary_requests, start=1):
            linked_network: dict[str, Any] | None
            association: dict[str, Any]
            if stream_capture:
                linked_network, association = cls._associate_stream_network_evidence(
                    request,
                    network_entries,
                )
            else:
                reqid = request.get("reqid")
                candidates = [
                    item
                    for item in network_entries
                    if isinstance(item.get("request_ids"), dict)
                    and item["request_ids"].get("reqid") == reqid
                ]
                linked_network = candidates[0] if len(candidates) == 1 else None
                association = {
                    "status": (
                        "matched"
                        if len(candidates) == 1
                        else "ambiguous"
                        if candidates
                        else "not_found"
                    ),
                    "method": "reqid" if candidates else None,
                    "candidate_count": len(candidates),
                }
            stable_id = (
                request.get("persistentRequestId")
                or request.get("cdpRequestId")
                or request.get("networkRequestId")
                or request.get("reqid")
                or ordinal
            )
            observation_id = evidence_id(
                experiment_id,
                "network_observation",
                stable_id=stable_id,
            ).replace("ev_", "obs_", 1)
            observation = build_network_observation(
                observation_id=observation_id,
                network_evidence=linked_network,
                stream_request=request if stream_capture else None,
                association=association,
            )
            observations.append(observation)
            request["networkObservationId"] = observation_id
            if linked_network is not None:
                linked_network["network_observation_id"] = observation_id
                summary = linked_network.get("summary")
                if isinstance(summary, dict):
                    summary.pop("snapshot_integrity", None)
        return observations

    @classmethod
    def _extract_http_status(cls, value: Any) -> int | None:
        if isinstance(value, dict):
            status = value.get("status")
            if isinstance(status, int):
                return status
            for child in value.values():
                found = cls._extract_http_status(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._extract_http_status(child)
                if found is not None:
                    return found
        elif isinstance(value, str):
            try:
                return cls._extract_http_status(json.loads(value))
            except json.JSONDecodeError:
                return None
        return None

    @classmethod
    def _extract_response_content_type(cls, value: Any) -> str | None:
        if isinstance(value, dict):
            headers = value.get("headers")
            if isinstance(headers, list):
                for item in headers:
                    if (
                        isinstance(item, list)
                        and len(item) >= 2
                        and str(item[0]).lower() == "content-type"
                    ):
                        return str(item[1]).split(";", 1)[0].strip().lower()
                    if (
                        isinstance(item, dict)
                        and str(item.get("name", "")).lower() == "content-type"
                    ):
                        return str(item.get("value", "")).split(";", 1)[0].strip().lower()
            for child in value.values():
                found = cls._extract_response_content_type(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._extract_response_content_type(child)
                if found:
                    return found
        elif isinstance(value, str):
            try:
                return cls._extract_response_content_type(json.loads(value))
            except json.JSONDecodeError:
                return None
        return None

    @classmethod
    def _stream_response_contract(
        cls,
        replay_plan: dict[str, Any],
        replay_response: Any,
        *,
        status: int | None,
        content_type: str | None,
    ) -> dict[str, Any] | None:
        response_control = replay_plan.get("spec", {}).get("responseControl", {})
        response_control = response_control if isinstance(response_control, dict) else {}
        response_mode = str(response_control.get("responseMode") or "auto")
        observed_mode = cls._extract_response_field(replay_response, "responseMode")
        observed_mode = str(observed_mode) if observed_mode is not None else None
        stream_modes = {"sse", "ndjson", "raw_stream"}
        valid_observed_modes = {"ordinary", *stream_modes}
        known_ndjson_types = {
            "application/x-ndjson",
            "application/ndjson",
        }
        normalized_content_type = (
            str(content_type).split(";", 1)[0].strip().lower()
            if content_type
            else None
        )
        auto_selected_mode = (
            "sse"
            if normalized_content_type == "text/event-stream"
            else "ndjson"
            if normalized_content_type in known_ndjson_types
            else "ordinary"
        )
        contract_expected = bool(
            response_mode in stream_modes
            or observed_mode in stream_modes
            or replay_plan.get("source_is_stream") is True
            or auto_selected_mode in stream_modes
        )
        if not contract_expected:
            return None
        terminal_conditions = response_control.get("terminalConditions")
        terminal_conditions = (
            [item for item in terminal_conditions if isinstance(item, dict)]
            if isinstance(terminal_conditions, list)
            else []
        )
        marker = response_control.get("doneMarker")
        event_name = response_control.get("doneEventName")
        termination = cls._extract_response_field(replay_response, "terminationReason")
        terminal_condition_matched = cls._extract_response_field(
            replay_response,
            "terminalConditionMatched",
        )
        marker_observed = bool(cls._extract_response_field(replay_response, "doneMarkerObserved"))
        observed_event_name = cls._extract_response_field(
            replay_response,
            "doneEventNameObserved",
        )
        truncated = bool(cls._extract_response_field(replay_response, "truncated"))
        mode_ok: bool | None = None
        content_type_ok: bool | None = None
        terminal_reason_ok: bool | None = None
        terminal_match_ok: bool | None = None
        content_type_required = response_mode in {"auto", "ordinary"}
        if (
            isinstance(status, int)
            and status >= 400
            and observed_mode == "ordinary"
            and response_mode in {"auto", "ordinary"}
            and auto_selected_mode == "ordinary"
        ):
            contract_status = "not_applicable_non_stream_response"
        else:
            marker_ok = not marker or marker_observed
            event_ok = not event_name or observed_event_name == event_name
            terminal_types = {
                str(item.get("type")) for item in terminal_conditions if item.get("type")
            } or {"network_close"}
            terminal_reason_ok = bool(
                ("exact_sse_data" in terminal_types and termination == "done_marker")
                or ("text_pattern" in terminal_types and termination == "text_pattern")
                or (
                    "network_close" in terminal_types
                    and termination in {"network_close", "no_response_body"}
                )
                or ("idle_window" in terminal_types and termination == "idle_window")
            )
            expected_terminal_match = {
                "done_marker": "exact_sse_data",
                "text_pattern": "text_pattern",
                "network_close": "network_close",
                "no_response_body": "network_close",
                "idle_window": "idle_window",
            }.get(str(termination))
            terminal_match_ok = bool(
                expected_terminal_match
                and terminal_condition_matched == expected_terminal_match
            )
            mode_ok = bool(
                observed_mode in valid_observed_modes
                and (response_mode == "auto" or observed_mode == response_mode)
            )
            content_type_ok = observed_mode == auto_selected_mode
            complete = bool(
                mode_ok
                and (content_type_ok or not content_type_required)
                and marker_ok
                and event_ok
                and terminal_reason_ok
                and terminal_match_ok
                and not truncated
            )
            contract_status = "complete" if complete else "partial"
        return {
            "status": contract_status,
            "response_mode": response_mode,
            "observed_response_mode": observed_mode,
            "response_mode_matches": mode_ok,
            "content_type_matches_observed_mode": content_type_ok,
            "content_type_required_for_contract": content_type_required,
            "terminal_conditions": terminal_conditions,
            "terminal_condition_matched": terminal_condition_matched,
            "observed_termination": termination,
            "termination_reason_matches_conditions": terminal_reason_ok,
            "terminal_condition_matches_observed_termination": terminal_match_ok,
            "done_marker_required": bool(marker),
            "done_marker_observed": marker_observed,
            "done_event_name_required": event_name,
            "done_event_name_observed": observed_event_name,
            "truncated": truncated,
        }

    @staticmethod
    def _sha256_lines(values: list[str]) -> str:
        return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()

    @classmethod
    def _request_context_hashes(
        cls,
        snapshot: dict[str, Any] | None,
        *,
        ignored_cookie_names: list[str] | None = None,
        ignored_context_headers: list[str] | None = None,
        context_header_names: list[str] | None = None,
    ) -> dict[str, Any]:
        headers = snapshot.get("requestHeadersArray") if isinstance(snapshot, dict) else None
        dimensions = network_snapshot_dimensions(snapshot) if isinstance(snapshot, dict) else {}
        if (
            not isinstance(headers, list)
            or dimensions.get("request_headers_completeness") != "complete"
        ):
            return {
                "status": "unavailable",
                "cookie_name_value_sha256": None,
                "authorization_sha256": None,
                "csrf_header_sha256": None,
                "context_headers_sha256": None,
                "request_context_sha256": None,
            }
        ignored_cookies = {
            item.strip().lower() for item in (ignored_cookie_names or []) if item.strip()
        }
        ignored_headers = {
            item.strip().lower() for item in (ignored_context_headers or []) if item.strip()
        }
        selected_context_headers = {
            item.strip().lower()
            for item in (
                context_header_names
                or [
                    "authorization",
                    "proxy-authorization",
                    "x-csrf-token",
                    "x-xsrf-token",
                ]
            )
            if item.strip()
        }
        cookies: list[str] = []
        authorization: list[str] = []
        csrf: list[str] = []
        context_headers: list[str] = []
        for header_index, item in enumerate(headers):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().lower()
            value = str(item.get("value", ""))
            if name in ignored_headers:
                continue
            if name == "cookie":
                for segment_index, segment in enumerate(value.split(";")):
                    normalized_segment = segment.strip()
                    if not normalized_segment:
                        continue
                    cookie_name = normalized_segment.split("=", 1)[0].strip().lower()
                    if cookie_name in ignored_cookies:
                        continue
                    cookies.append(f"{header_index}:{segment_index}:{normalized_segment}")
            elif name in {"authorization", "proxy-authorization"}:
                authorization.append(f"{header_index}:{name}:{value}")
            elif "csrf" in name or "xsrf" in name:
                csrf.append(f"{header_index}:{name}:{value}")
            if name in selected_context_headers:
                context_headers.append(f"{header_index}:{name}:{value}")
        cookie_hash = cls._sha256_lines(cookies)
        authorization_hash = cls._sha256_lines(authorization)
        csrf_hash = cls._sha256_lines(csrf)
        context_headers_hash = cls._sha256_lines(context_headers)
        return {
            "status": "observed",
            "cookie_name_value_sha256": cookie_hash,
            "authorization_sha256": authorization_hash,
            "csrf_header_sha256": csrf_hash,
            "context_headers_sha256": context_headers_hash,
            "request_context_sha256": cls._sha256_lines(
                [
                    f"cookie:{cookie_hash}",
                    f"context_headers:{context_headers_hash}",
                ]
            ),
            "ignored_cookie_names": sorted(ignored_cookies),
            "ignored_context_headers": sorted(ignored_headers),
            "context_header_names": sorted(selected_context_headers),
        }

    @classmethod
    def _environment_fingerprint(
        cls,
        alignment: AlignmentResult | None,
        wire_snapshot: dict[str, Any] | None,
        *,
        phase: str,
        include_request_context: bool = True,
        ignored_cookie_names: list[str] | None = None,
        ignored_context_headers: list[str] | None = None,
        context_header_names: list[str] | None = None,
    ) -> dict[str, Any]:
        page_id = alignment.js_reverse_page_id if alignment is not None else None
        page_url = (
            alignment.js_reverse_page_url or alignment.playwright_page.url
            if alignment is not None
            else None
        )
        page_split = urlsplit(page_url or "")
        request_url = str(wire_snapshot.get("url", "")) if isinstance(wire_snapshot, dict) else ""
        request_split = urlsplit(request_url)
        request_context = (
            cls._request_context_hashes(
                wire_snapshot,
                ignored_cookie_names=ignored_cookie_names,
                ignored_context_headers=ignored_context_headers,
                context_header_names=context_header_names,
            )
            if include_request_context
            else {
                "status": "not_applicable",
                "cookie_name_value_sha256": None,
                "authorization_sha256": None,
                "csrf_header_sha256": None,
                "context_headers_sha256": None,
                "request_context_sha256": None,
                "ignored_cookie_names": sorted(ignored_cookie_names or []),
                "ignored_context_headers": sorted(ignored_context_headers or []),
                "context_header_names": sorted(context_header_names or []),
            }
        )
        unavailable: list[str] = []
        if alignment is None or alignment.status != "aligned":
            unavailable.extend(["page_id", "page_url", "page_origin"])
        if include_request_context and request_context["status"] != "observed":
            unavailable.append("request_context_sha256")
        return {
            "phase": phase,
            "page_id": page_id,
            "page_url": page_url,
            "page_origin": (
                f"{page_split.scheme}://{page_split.netloc}"
                if page_split.scheme and page_split.netloc
                else None
            ),
            "request_origin": (
                f"{request_split.scheme}://{request_split.netloc}"
                if request_split.scheme and request_split.netloc
                else None
            ),
            "request_path": request_split.path or None,
            **request_context,
            "unavailable_dimensions": sorted(set(unavailable)),
        }

    @staticmethod
    def _compare_environment_facts(
        reference: dict[str, Any] | None,
        current: dict[str, Any] | None,
        dimensions: list[str],
    ) -> dict[str, Any]:
        return compare_environment_facts(reference, current, dimensions)

    @staticmethod
    def _stream_summary_from_observation(observation: dict[str, Any]) -> dict[str, Any] | None:
        return stream_summary_from_observation(observation)

    @classmethod
    def _current_replay_stream_summary(
        cls,
        observations: list[dict[str, Any]],
        replay_network_evidence_id: str | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        return select_current_stream_summary(
            observations,
            replay_network_evidence_id,
        )

    def _comparison_reference_facts(
        self,
        manifest: dict[str, Any],
        reference: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, str], str | None]:
        observations = manifest.get("network_observations")
        observations = (
            [item for item in observations if isinstance(item, dict)]
            if isinstance(observations, list)
            else []
        )
        evidence_id_value = reference.get("evidence_id")
        observation_id_value = reference.get("observation_id")
        overrides: dict[str, str] = {}
        if isinstance(observation_id_value, str):
            matches = [
                item
                for item in observations
                if item.get("observation_id") == observation_id_value
            ]
            if not matches:
                return None, {}, "observation_not_found"
            if len(matches) > 1:
                return None, {}, "observation_ambiguous"
            observation = matches[0]
            facts = observation.get("facts")
            facts = facts if isinstance(facts, dict) else {}
            sources = observation.get("sources")
            sources = sources if isinstance(sources, dict) else {}
            linked_evidence_id = sources.get("network_evidence_id")
            snapshot = None
            if isinstance(linked_evidence_id, str):
                try:
                    linked_evidence = self._find_evidence(manifest, linked_evidence_id)
                    snapshot = self._network_evidence_snapshot(
                        self.experiments.root,
                        linked_evidence,
                    )
                except BrowserServiceError:
                    snapshot = None
            return (
                {
                    "request_body": facts.get("request_body_canonical_sha256"),
                    "response_status": facts.get("http_status"),
                    "response_content_type": (
                        response_content_type(snapshot)
                        if isinstance(snapshot, dict)
                        else None
                    ),
                    "stream_summary": self._stream_summary_from_observation(observation),
                    "environment": manifest.get("pre_dispatch_environment"),
                },
                overrides,
                None,
            )
        if not isinstance(evidence_id_value, str):
            return None, {}, "reference_selector_missing"
        try:
            evidence = self._find_evidence(manifest, evidence_id_value)
        except BrowserServiceError as exc:
            return None, {}, exc.code
        if evidence.get("kind") != "network_request":
            return None, {}, "comparison_evidence_kind_invalid"
        summary = evidence.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        snapshot = self._network_evidence_snapshot(self.experiments.root, evidence)
        linked_observations = [
            item
            for item in observations
            if isinstance(item.get("sources"), dict)
            and item["sources"].get("network_evidence_id") == evidence_id_value
        ]
        stream_summary = None
        if len(linked_observations) == 1:
            stream_summary = self._stream_summary_from_observation(linked_observations[0])
        elif len(linked_observations) > 1:
            overrides["stream_summary"] = "ambiguous"
        return (
            {
                "request_body": (
                    evidence.get("request_body_canonical_sha256")
                    or (
                        request_body_canonical_sha256_from_snapshot(snapshot)
                        if isinstance(snapshot, dict)
                        else None
                    )
                ),
                "response_status": (
                    snapshot.get("status")
                    if isinstance(snapshot, dict)
                    else summary.get("status")
                ),
                "response_content_type": (
                    response_content_type(snapshot)
                    if isinstance(snapshot, dict)
                    else None
                ),
                "stream_summary": stream_summary,
                "environment": manifest.get("pre_dispatch_environment"),
            },
            overrides,
            None,
        )

    def _build_replay_comparison_results(
        self,
        replay_plan: dict[str, Any],
        *,
        current_request_body_sha256: str | None,
        current_response_status: int | None,
        current_response_content_type: str | None,
        current_stream_facts: dict[str, Any] | None,
        current_environment: dict[str, Any] | None,
        current_status_overrides: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        comparison = replay_plan.get("comparison")
        if not isinstance(comparison, dict):
            return []
        references = [
            dict(item)
            for item in comparison.get("references", [])
            if isinstance(item, dict)
        ]
        if comparison.get("include_source") is True:
            references.insert(
                0,
                {
                    "experiment_id": str(replay_plan["source_experiment_id"]),
                    "evidence_id": str(replay_plan["source_evidence_id"]),
                },
            )
        unique_references: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
        for item in references:
            key = (
                str(item.get("experiment_id") or ""),
                str(item.get("evidence_id")) if item.get("evidence_id") else None,
                str(item.get("observation_id")) if item.get("observation_id") else None,
            )
            unique_references[key] = item
        references = list(unique_references.values())
        dimensions = [str(item) for item in comparison.get("dimensions", [])]
        environment = comparison.get("environment")
        environment = environment if isinstance(environment, dict) else {}
        environment_dimensions = [str(item) for item in environment.get("dimensions", [])]
        current = {
            "request_body": current_request_body_sha256,
            "response_status": current_response_status,
            "response_content_type": current_response_content_type,
            "stream_summary": current_stream_facts,
            "environment": current_environment,
        }
        current_status_overrides = current_status_overrides or {}
        results: list[dict[str, Any]] = []
        for reference_selector in references:
            reference_id = str(reference_selector.get("experiment_id") or "")
            try:
                reference_manifest = self.experiments.load_manifest(reference_id)
            except BrowserServiceError as exc:
                results.append(
                    {
                        "reference_experiment_id": reference_id,
                        "reference": reference_selector,
                        "status": "missing",
                        "error": exc.code,
                        "dimensions": {},
                    }
                )
                continue
            reference, status_overrides, selection_error = self._comparison_reference_facts(
                reference_manifest,
                reference_selector,
            )
            if reference is None:
                results.append(
                    {
                        "reference_experiment_id": reference_id,
                        "reference": reference_selector,
                        "status": (
                            "ambiguous"
                            if selection_error and "ambiguous" in selection_error
                            else "missing"
                        ),
                        "error": selection_error,
                        "dimensions": {},
                    }
                )
                continue
            dimension_results: dict[str, Any] = {}
            for dimension in dimensions:
                if dimension == "environment":
                    dimension_results[dimension] = self._compare_environment_facts(
                        reference.get("environment"),
                        current.get("environment"),
                        environment_dimensions,
                    )
                    continue
                reference_value = reference.get(dimension)
                current_value = current.get(dimension)
                dimension_results[dimension] = compare_dimension(
                    reference_value,
                    current_value,
                    reference_status=status_overrides.get(dimension),
                    current_status=current_status_overrides.get(dimension),
                )
            results.append(
                {
                    "reference_experiment_id": reference_id,
                    "reference": reference_selector,
                    "status": aggregate_dimension_status(dimension_results),
                    "dimensions": dimension_results,
                }
            )
        return results

    @staticmethod
    def _stream_evidence_entries(
        experiment_id: str,
        primary_requests: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for ordinal, request in enumerate(primary_requests, start=1):
            persistent_id = request.get("persistentRequestId")
            cdp_id = request.get("cdpRequestId")
            core_artifacts = request.get("coreArtifacts")
            core_artifacts = (
                [item for item in core_artifacts if isinstance(item, dict)]
                if isinstance(core_artifacts, list)
                else []
            )
            artifact_ids = [
                str(item.get("artifactId")) for item in core_artifacts if item.get("artifactId")
            ]
            artifact_paths = {
                str(item.get("kind") or item.get("artifactId")): str(item.get("relativePath"))
                for item in core_artifacts
                if item.get("relativePath")
            }
            stream_id = evidence_id(
                experiment_id,
                "stream_request",
                stable_id=persistent_id or cdp_id or ordinal,
            )
            entries.append(
                {
                    "evidence_id": stream_id,
                    "kind": "stream_request",
                    "network_observation_id": request.get("networkObservationId"),
                    "request_ids": {
                        "persistent": persistent_id,
                        "cdp": cdp_id,
                        "network": request.get("networkRequestId"),
                        "collector_generation": request.get("collectorGeneration"),
                    },
                    "artifact_ids": artifact_ids,
                    "artifact_paths": artifact_paths,
                    "summary": {
                        "url": str(request.get("url", ""))[:8192],
                        "method": request.get("method"),
                        "status": request.get("status"),
                        "terminal_reason": request.get("terminalReason"),
                        "primary_event_source": request.get("primaryEventSource"),
                        "raw_event_count": request.get("rawEventCount"),
                        "semantic_event_count": request.get("semanticEventCount"),
                        "raw_capture_integrity": request.get("rawCaptureIntegrity"),
                        "semantic_parse_integrity": request.get("semanticParseIntegrity"),
                        "stream_artifact_integrity": request.get("artifactIntegrity"),
                    },
                }
            )
            ranges = [
                (
                    "raw-stream",
                    int(request.get("rawEventCount") or 0),
                    {"events", "decoded_sse", "chunks"},
                ),
                (
                    "eventsource",
                    int(request.get("semanticEventCount") or 0),
                    {"eventsource_events"},
                ),
            ]
            for event_source, event_count, artifact_kinds in ranges:
                event_artifacts = [
                    str(item.get("artifactId"))
                    for item in core_artifacts
                    if str(item.get("kind", "")) in artifact_kinds and item.get("artifactId")
                ]
                if event_count <= 0 or not event_artifacts:
                    continue
                entries.append(
                    {
                        "evidence_id": evidence_id(
                            experiment_id,
                            "stream_event_range",
                            selector_id=event_source,
                            stable_id=persistent_id or cdp_id or ordinal,
                        ),
                        "kind": "stream_event_range",
                        "stream_request_evidence_id": stream_id,
                        "event_source": event_source,
                        "start_event_index": 0,
                        "end_event_index": event_count - 1,
                        "artifact_ids": event_artifacts,
                    }
                )
        return entries

    @staticmethod
    def _collect_artifacts(*payloads: dict[str, Any]) -> list[dict[str, Any]]:
        artifacts: dict[str, dict[str, Any]] = {}

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                artifact_id = value.get("artifactId") or value.get("artifact_id")
                relative_path = value.get("relativePath") or value.get("relative_path")
                if artifact_id and relative_path:
                    artifacts[str(artifact_id)] = value
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for payload in payloads:
            visit(payload)
        return list(artifacts.values())
    async def _collect_post_flow_evidence(
        self,
        *,
        experiment_id: str,
        experiment_dir: Path,
        manifest: dict[str, Any],
        payload: CaptureFlowPayload,
        network_payload: dict[str, Any],
        network_checkpoint_value: dict[str, Any],
        request_matcher: RequestMatcher,
        canceled: bool,
        step_results: list[FlowStepResult],
        console_checkpoint_value: dict[str, Any],
        warnings: list[str],
    ) -> EvidenceCollectionResult:
        raw_network_requests = network_payload.get("requests")
        raw_network_requests = (
            [item for item in raw_network_requests if isinstance(item, dict)]
            if isinstance(raw_network_requests, list)
            else []
        )
        window_requests = requests_after_checkpoint(
            raw_network_requests,
            network_checkpoint_value,
            include_in_flight=payload.primary_request.include_in_flight,
        )
        network_payload["requests"] = window_requests
        network_payload["window"] = {
            **network_checkpoint_value,
            "matched_request_count": len(window_requests),
            "collector_generation_at_finalize": self._transport_generation(),
        }
        primary_network_payload = {
            **network_payload,
            "requests": [
                item
                for item in window_requests
                if network_request_matches(item, request_matcher)
            ],
        }
        evidence_entries = self._evidence_index(manifest)
        evidence_artifacts: list[dict[str, Any]] = []
        if (
            not canceled
            and payload.network_evidence
            and self._transport_generation()
            == int(
                network_checkpoint_value.get(
                    "collector_generation", self._transport_generation()
                )
            )
        ):
            try:
                (
                    exported_entries,
                    exported_artifacts,
                    export_warnings,
                ) = await self._export_network_evidence(
                    experiment_id=experiment_id,
                    experiment_dir=experiment_dir,
                    selectors=payload.network_evidence,
                    requests=window_requests,
                    deadline=Deadline(8_000),
                    step_ids=[
                        item.step_id for item in step_results if item.status == "completed"
                    ],
                )
                evidence_entries.extend(exported_entries)
                evidence_artifacts.extend(exported_artifacts)
                warnings.extend(export_warnings)
            except Exception as exc:
                warnings.append(f"network evidence export: {str(exc)[:3000]}")
        if not canceled and payload.capture.console_errors:
            (
                console_entries,
                console_artifacts,
                console_warnings,
            ) = await self._export_console_evidence(
                experiment_id=experiment_id,
                experiment_dir=experiment_dir,
                checkpoint=console_checkpoint_value,
                deadline=Deadline(4_000),
            )
            evidence_entries.extend(console_entries)
            evidence_artifacts.extend(console_artifacts)
            warnings.extend(console_warnings)
        return EvidenceCollectionResult(
            network_payload=network_payload,
            primary_network_payload=primary_network_payload,
            evidence_entries=evidence_entries,
            artifacts=evidence_artifacts,
        )
    def _assemble_observations_stage(
        self,
        *,
        payload: CaptureFlowPayload,
        replay_plan: dict[str, Any] | None,
        experiment_id: str,
        evidence_entries: list[dict[str, Any]],
        final_status_payload: dict[str, Any],
        primary_network_payload: dict[str, Any],
        replay_network_evidence_id: str | None,
        replay_observed_response_mode: str | None,
        stream_response_contract: dict[str, Any] | None,
        response_evidence_source: str | None,
        step_results: list[FlowStepResult],
        alignment: AlignmentResult,
        post_alignment: AlignmentResult,
        wait_observations: list[dict[str, Any]],
        wire_snapshot: dict[str, Any] | None,
        replay_http_status: int | None,
        replay_response_content_type: str | None,
        pre_dispatch_environment: dict[str, Any] | None,
        errors: list[str],
    ) -> ObservationAssemblyResult:
        network_evidence_entries = [
            item for item in evidence_entries if item.get("kind") == "network_request"
        ]
        observed_stream_response = replay_observed_response_mode in {
            "sse",
            "ndjson",
            "raw_stream",
        }
        configured_response_mode = (
            str(
                replay_plan.get("spec", {})
                .get("responseControl", {})
                .get("responseMode", "auto")
            )
            if replay_plan is not None
            else "ordinary"
        )
        non_stream_error_response_observed = bool(
            replay_plan is not None
            and payload.capture.stream
            and isinstance(replay_http_status, int)
            and replay_http_status >= 400
            and replay_observed_response_mode == "ordinary"
            and configured_response_mode in {"auto", "ordinary"}
            and (
                stream_response_contract is None
                or stream_response_contract.get("status")
                == "not_applicable_non_stream_response"
            )
            and replay_network_evidence_id
        )
        stream_evidence_required = (
            observed_stream_response
            if replay_plan is not None
            else payload.capture.stream
        )
        primary_status_payload = final_status_payload
        if (
            replay_plan is not None
            and observed_stream_response
            and replay_network_evidence_id
            and not non_stream_error_response_observed
        ):
            locked_stream_requests: list[dict[str, Any]] = []
            for item in final_status_payload.get("requests", []):
                if not isinstance(item, dict):
                    continue
                linked_network, _ = self._associate_stream_network_evidence(
                    item,
                    network_evidence_entries,
                )
                if (
                    isinstance(linked_network, dict)
                    and linked_network.get("evidence_id") == replay_network_evidence_id
                ):
                    locked_stream_requests.append(item)
            primary_status_payload = {
                **final_status_payload,
                "requests": locked_stream_requests,
            }
            if len(locked_stream_requests) != 1:
                errors.append(
                    "Replay primary stream could not be locked to exactly one "
                    "networkRequestId + collectorGeneration association."
                )

        primary_requests, count_ok = self._select_primary_requests(
            payload,
            primary_status_payload,
            primary_network_payload,
        )
        if replay_plan is not None and not observed_stream_response:
            primary_requests = list(primary_network_payload["requests"])
            count_ok = (
                payload.primary_request.expected_min_matches
                <= len(primary_requests)
                <= payload.primary_request.expected_max_matches
            )
        cancellation_classifications = self._classify_cancellations(
            payload,
            step_results,
            primary_requests,
            alignment,
            post_alignment,
            wait_observations,
        )
        network_observations = self._build_network_observations(
            experiment_id,
            primary_requests,
            network_evidence_entries,
            stream_capture=stream_evidence_required,
        )
        if (
            non_stream_error_response_observed
            and response_evidence_source == "complete_replay_response_body"
        ):
            for observation in network_observations:
                completeness = observation.get("completeness")
                if not isinstance(completeness, dict):
                    continue
                completeness["response_body"] = "complete"
                missing = observation.get("missing_evidence")
                if isinstance(missing, list):
                    observation["missing_evidence"] = [
                        item for item in missing if item != "response_body"
                    ]
        if stream_evidence_required:
            evidence_entries.extend(
                self._stream_evidence_entries(
                    experiment_id,
                    primary_requests,
                )
            )
        extractor_observations = (
            replay_plan.get("extractor_observations", []) if replay_plan is not None else []
        )
        extractor_observations = (
            [item for item in extractor_observations if isinstance(item, dict)]
            if isinstance(extractor_observations, list)
            else []
        )
        for ordinal, observation in enumerate(extractor_observations, start=1):
            evidence_entries.append(
                {
                    "evidence_id": evidence_id(
                        experiment_id,
                        "replay_extractor",
                        stable_id=observation.get("extractor_id") or ordinal,
                    ),
                    "kind": "replay_extractor",
                    "step_ids": ["replay_request"],
                    "artifact_ids": observation.get("artifact_ids", []),
                    "summary": observation,
                }
            )
        comparison_results = self._build_replay_comparisons_stage(
            replay_plan=replay_plan,
            network_observations=[
                item for item in network_observations if isinstance(item, dict)
            ],
            replay_network_evidence_id=replay_network_evidence_id,
            wire_snapshot=wire_snapshot,
            replay_http_status=replay_http_status,
            replay_response_content_type=replay_response_content_type,
            pre_dispatch_environment=pre_dispatch_environment,
        )
        return ObservationAssemblyResult(
            primary_requests=primary_requests,
            count_satisfied=count_ok,
            cancellation_classifications=cancellation_classifications,
            network_observations=network_observations,
            comparison_results=comparison_results,
            extractor_observations=extractor_observations,
            observed_stream_response=observed_stream_response,
            non_stream_error_response_observed=non_stream_error_response_observed,
            stream_evidence_required=stream_evidence_required,
        )
