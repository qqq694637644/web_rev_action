"""Read-only session, experiment, source, and evidence inspection."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from ...browser_models import (
    BrowserActionResponse,
    CaptureFlowPayload,
    GetExperimentRequest,
    GetNetworkEvidenceRequest,
    GetRequestInitiatorRequest,
    GetRequestShapeRequest,
    GetScriptSourceRequest,
    GetSessionRequest,
    GetStreamStatusRequest,
    InspectBrowserEvidenceRequest,
    ListConsoleErrorsRequest,
    ListEvidenceRequest,
    ListExperimentsRequest,
    SaveScriptSourceRequest,
    SearchScriptsRequest,
)
from ...protocol_evidence import evidence_id
from ..core import BrowserServiceError, Deadline, utc_now
from ..registry import OPERATION_REGISTRY
from ..session_states import STALE_ON_SERVICE_CHANGE


class BrowserInspectionOperations:
    """Own inspection behavior while the public service remains a facade."""

    async def _save_script_source(
        self,
        request: SaveScriptSourceRequest,
    ) -> BrowserActionResponse:
        payload = request.payload
        manifest = self.experiments.load_manifest(payload.target_experiment_id)
        if manifest.get("session_id") != payload.session_id:
            raise BrowserServiceError(
                "script_target_session_mismatch",
                "The target experiment belongs to a different browser session.",
                409,
            )
        if payload.initiator_evidence_id:
            initiator = self._find_evidence(
                manifest,
                payload.initiator_evidence_id,
            )
            if initiator.get("kind") != "network_request":
                raise BrowserServiceError(
                    "initiator_evidence_kind_invalid",
                    "initiator_evidence_id must reference network_request evidence.",
                    409,
                )

        async def source(deadline: Deadline) -> dict[str, Any]:
            return await self.js_reverse.get_script_source(
                deadline,
                url=payload.url,
                script_id=payload.script_id,
                start_line=payload.start_line,
                end_line=payload.end_line,
                offset=payload.offset,
                length=payload.length,
            )

        result = await self._run_aligned_inspection(
            session_id=payload.session_id,
            operation=request.operation,
            callback=source,
        )
        source_text = result.get("source") or result.get("scriptSource")
        if not isinstance(source_text, str):
            source_text = json.dumps(result, ensure_ascii=False, indent=2)
        digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        label = (
            re.sub(
                r"[^A-Za-z0-9_.-]+",
                "-",
                payload.evidence_label or payload.script_id or payload.url or "script",
            ).strip("-._")[:80]
            or "script"
        )
        ev_id = evidence_id(
            payload.target_experiment_id,
            "script_source",
            selector_id=label,
            stable_id=digest[:16],
        )
        source_dir = (
            self.experiments.root
            / "experiments"
            / payload.target_experiment_id
            / "js-reverse"
            / "sources"
        )
        source_dir.mkdir(parents=True, exist_ok=True)
        source_file = source_dir / f"{ev_id}.js"
        metadata_file = source_dir / f"{ev_id}.metadata.json"
        source_file.write_text(source_text, encoding="utf-8")
        metadata = {
            "script_url": payload.url,
            "script_id": payload.script_id,
            "start_line": payload.start_line,
            "end_line": payload.end_line,
            "offset": payload.offset,
            "length": payload.length,
            "sha256": digest,
            "initiator_evidence_id": payload.initiator_evidence_id,
        }
        metadata_file.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        source_artifact_id = f"art_{ev_id}_source"
        metadata_artifact_id = f"art_{ev_id}_metadata"
        source_descriptor = self.experiments.describe_local_artifact(
            str(source_file),
            artifact_id=source_artifact_id,
            kind="script_source",
            sensitivity="private",
        )
        metadata_descriptor = self.experiments.describe_local_artifact(
            str(metadata_file),
            artifact_id=metadata_artifact_id,
            kind="script_source_metadata",
            sensitivity="private",
        )
        artifacts = [item for item in (source_descriptor, metadata_descriptor) if item is not None]
        evidence = {
            "evidence_id": ev_id,
            "kind": "script_source",
            "artifact_ids": [item["artifactId"] for item in artifacts],
            "artifact_paths": {item["kind"]: item["relativePath"] for item in artifacts},
            "initiator_evidence_id": payload.initiator_evidence_id,
            "script_url": payload.url,
            "script_id": payload.script_id,
            "sha256": digest,
            "range": {
                "start_line": payload.start_line,
                "end_line": payload.end_line,
                "offset": payload.offset,
                "length": payload.length,
            },
        }
        self._evidence_index(manifest).append(evidence)
        existing_artifacts = manifest.get("artifacts")
        if not isinstance(existing_artifacts, list):
            existing_artifacts = []
            manifest["artifacts"] = existing_artifacts
        existing_artifacts.extend(artifacts)
        if request.action_binding is not None:
            invocations = manifest.get("action_invocations")
            if not isinstance(invocations, list):
                invocations = []
                manifest["action_invocations"] = invocations
            invocations.append(
                {
                    **request.action_binding.model_dump(mode="json"),
                    "recorded_at": utc_now(),
                }
            )
        manifest["updated_at"] = utc_now()
        self.experiments.write_manifest(payload.target_experiment_id, manifest)
        return BrowserActionResponse(
            operation=request.operation,
            status="completed",
            session_id=payload.session_id,
            experiment_id=payload.target_experiment_id,
            result={"evidence": evidence},
        )

    @staticmethod
    def _manifest_relative_path(experiment_id: str) -> str:
        return (Path("experiments") / experiment_id / "manifest.json").as_posix()

    def _transport_generation(self) -> int:
        return int(getattr(self.js_reverse, "transport_generation", 0))

    def _discover_capture_metadata(
        self,
        experiment_id: str,
        manifest: dict[str, Any] | None = None,
        runtime_warnings: list[str] | None = None,
    ) -> dict[str, Any] | None:
        base = self.experiments.experiment_dir(experiment_id) / "js-reverse"
        candidates = sorted(
            base.glob("capture-*/capture.json"),
            key=lambda path: path.stat().st_mtime_ns if path.exists() else 0,
            reverse=True,
        )
        for path in candidates:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                relative = self.experiments.relative_path(str(path)) or path.as_posix()
                warning = (
                    f"capture metadata invalid: {relative}: "
                    f"{type(exc).__name__}: {str(exc)[:1000]}"
                )
                if runtime_warnings is not None and warning not in runtime_warnings:
                    runtime_warnings.append(warning)
                if manifest is not None:
                    warnings = manifest.get("warnings")
                    if not isinstance(warnings, list):
                        warnings = []
                        manifest["warnings"] = warnings
                    if warning not in warnings:
                        warnings.append(warning)
                        self.experiments.write_manifest(experiment_id, manifest)
                continue
            if not isinstance(value, dict):
                relative = self.experiments.relative_path(str(path)) or path.as_posix()
                warning = (
                    f"capture metadata invalid: {relative}: "
                    "TypeError: expected JSON object"
                )
                if runtime_warnings is not None and warning not in runtime_warnings:
                    runtime_warnings.append(warning)
                if manifest is not None:
                    warnings = manifest.get("warnings")
                    if not isinstance(warnings, list):
                        warnings = []
                        manifest["warnings"] = warnings
                    if warning not in warnings:
                        warnings.append(warning)
                        self.experiments.write_manifest(experiment_id, manifest)
                continue
            relative = self.experiments.relative_path(str(path.parent))
            return {
                "capture_id": value.get("captureId"),
                "capture_uuid": value.get("captureUuid"),
                "capture_relative_dir": relative,
                "capture_metadata_artifact_id": (
                    (value.get("metadataArtifact") or {}).get("artifactId")
                    if isinstance(value.get("metadataArtifact"), dict)
                    else None
                ),
                "capture_metadata_relative_path": self.experiments.relative_path(str(path)),
            }
        return None

    @staticmethod
    def _manifest_stream_runtime(manifest: dict[str, Any]) -> dict[str, Any]:
        runtime = manifest.get("stream_runtime")
        if isinstance(runtime, dict):
            return dict(runtime)
        health = manifest.get("capture_health")
        health = health if isinstance(health, dict) else {}
        return {
            "capture_id": manifest.get("stream_capture_id"),
            "capture_uuid": health.get("capture_uuid"),
            "capture_relative_dir": health.get("capture_relative_dir"),
            "capture_metadata_artifact_id": health.get("capture_metadata_artifact_id"),
            "transport_generation": health.get("transport_generation"),
            "start_status": health.get("stream_start_status"),
        }

    @staticmethod
    def _evidence_index(manifest: dict[str, Any]) -> list[dict[str, Any]]:
        value = manifest.get("evidence")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        manifest["evidence"] = []
        return manifest["evidence"]

    @classmethod
    def _find_evidence(cls, manifest: dict[str, Any], target_evidence_id: str) -> dict[str, Any]:
        for item in cls._evidence_index(manifest):
            if item.get("evidence_id") == target_evidence_id:
                return item
        raise BrowserServiceError(
            "evidence_not_found",
            f"Evidence was not found: {target_evidence_id}",
            404,
        )

    @classmethod
    def _filter_shape_paths(
        cls,
        paths: dict[str, Any],
        *,
        path_prefix: str,
        max_depth: int,
        max_array_items: int,
    ) -> list[tuple[str, Any]]:
        prefix_tokens = cls._pointer_tokens(path_prefix)
        selected: list[tuple[str, Any]] = []
        for path, descriptor in paths.items():
            if not isinstance(path, str):
                continue
            tokens = cls._pointer_tokens(path)
            if tokens[: len(prefix_tokens)] != prefix_tokens:
                continue
            relative = tokens[len(prefix_tokens) :]
            if len(relative) > max_depth:
                continue
            if any(token.isdigit() and int(token) >= max_array_items for token in relative):
                continue
            selected.append((path, descriptor))
        return sorted(selected, key=lambda item: item[0])

    @classmethod
    def _bounded_redacted_subtree(
        cls,
        value: Any,
        *,
        path_prefix: str,
        max_depth: int,
        max_array_items: int,
    ) -> Any:
        current = value
        for token in cls._pointer_tokens(path_prefix):
            if isinstance(current, dict) and token in current:
                current = current[token]
            elif isinstance(current, list) and token.isdigit():
                index = int(token)
                if index >= len(current):
                    return None
                current = current[index]
            else:
                return None

        def prune(item: Any, depth: int) -> Any:
            if depth >= max_depth:
                if isinstance(item, dict):
                    return {"$truncated": "object"}
                if isinstance(item, list):
                    return {"$truncated": "array", "length": len(item)}
                return item
            if isinstance(item, dict):
                keys = sorted(item)[:100]
                result = {key: prune(item[key], depth + 1) for key in keys}
                if len(item) > len(keys):
                    result["$truncated_key_count"] = len(item) - len(keys)
                return result
            if isinstance(item, list):
                result = [prune(child, depth + 1) for child in item[:max_array_items]]
                if len(item) > max_array_items:
                    result.append({"$truncated_array_items": len(item) - max_array_items})
                return result
            return item

        return prune(current, 0)

    def _validate_and_store_series(
        self,
        *,
        session_id: str,
        manifest: dict[str, Any],
        payload: CaptureFlowPayload,
    ) -> None:
        series = payload.series.model_dump(mode="json", exclude_none=True)
        predecessor_id = series.get("predecessor_experiment_id")
        if predecessor_id:
            predecessor = self.experiments.load_manifest(str(predecessor_id))
            if predecessor.get("session_id") != session_id:
                raise BrowserServiceError(
                    "predecessor_session_mismatch",
                    "The predecessor experiment belongs to a different session.",
                    409,
                )
            predecessor_series = predecessor.get("series")
            predecessor_series = predecessor_series if isinstance(predecessor_series, dict) else {}
            requested_series = series.get("analysis_series_id")
            existing_series = predecessor_series.get("analysis_series_id")
            if requested_series and existing_series and requested_series != existing_series:
                raise BrowserServiceError(
                    "predecessor_series_mismatch",
                    "The predecessor experiment belongs to a different analysis series.",
                    409,
                )
        manifest["series"] = series

    @staticmethod
    def _experiment_summary(manifest: dict[str, Any]) -> dict[str, Any]:
        observations = manifest.get("network_observations")
        observation_summaries: list[dict[str, Any]] = []
        if isinstance(observations, list):
            for observation in observations[:10]:
                if not isinstance(observation, dict):
                    continue
                facts = observation.get("facts")
                facts = facts if isinstance(facts, dict) else {}
                observation_summaries.append(
                    {
                        "observation_id": observation.get("observation_id"),
                        "url": str(facts.get("url", ""))[:2048],
                        "method": facts.get("method"),
                        "http_status": facts.get("http_status"),
                        "request_lifecycle_status": facts.get(
                            "request_lifecycle_status"
                        ),
                        "association": observation.get("association"),
                        "completeness": observation.get("completeness"),
                        "missing_evidence": observation.get("missing_evidence"),
                    }
                )
        health = manifest.get("capture_health")
        return {
            "experiment_id": manifest.get("experiment_id"),
            "session_id": manifest.get("session_id"),
            "operation": manifest.get("operation"),
            "status": manifest.get("status"),
            "execution": manifest.get("execution"),
            "quality_summary": manifest.get("quality_summary"),
            "comparison_results": manifest.get("comparison_results"),
            "network_observations": observation_summaries,
            "network_observation_count": (
                len(observations) if isinstance(observations, list) else 0
            ),
            "capture_health": dict(health) if isinstance(health, dict) else {},
            "series": (
                dict(manifest["series"]) if isinstance(manifest.get("series"), dict) else {}
            ),
            "evidence_count": len(manifest.get("evidence", []))
            if isinstance(manifest.get("evidence"), list)
            else 0,
            "warnings": [str(item)[:1000] for item in manifest.get("warnings", [])[:10]],
            "errors": [str(item)[:1000] for item in manifest.get("errors", [])[:10]],
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
        }

    def _get_session(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id) or self.experiments.load_session(session_id)
        if not session:
            raise BrowserServiceError("session_not_found", "Browser session was not found", 404)
        if (
            session.get("status") in STALE_ON_SERVICE_CHANGE
            and session.get("service_instance_id") != self.service_instance_id
        ):
            session["previous_status"] = session.get("status")
            session["status"] = "stale"
            session["stale_reason"] = "service_instance_changed"
            session["updated_at"] = utc_now()
            self.experiments.save_session(session)
        self.sessions[session_id] = session
        return session

    async def inspect(self, request: InspectBrowserEvidenceRequest) -> BrowserActionResponse:
        spec = OPERATION_REGISTRY.require(request.operation)
        if spec.action != "inspect":
            raise BrowserServiceError(
                "unsupported_operation", "Unsupported inspect operation", 400
            )
        handler = getattr(self, spec.handler_name, None)
        if not callable(handler):
            raise RuntimeError(
                f"Operation registry handler is unavailable: "
                f"{spec.name} -> {spec.handler_name}"
            )
        return await handler(request)

    async def _inspect_get_session(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_list_experiments(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_get_experiment(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_get_stream_status(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_list_evidence(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_get_network_evidence(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_get_request_shape(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_get_request_initiator(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_search_scripts(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_get_script_source(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_list_console_errors(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        return await self._inspect_registered(request)

    async def _inspect_registered(
        self, request: InspectBrowserEvidenceRequest
    ) -> BrowserActionResponse:
        if isinstance(request, GetSessionRequest):
            session = self._get_session(request.payload.session_id)
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=request.payload.session_id,
                result={"session": session},
            )
        if isinstance(request, ListExperimentsRequest):
            listing = self.experiments.list_experiments(
                request.payload.session_id, request.payload.limit
            )
            items = listing["experiments"]
            manifest_errors = listing["manifest_errors"]
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                result={
                    "experiments": items,
                    "count": len(items),
                    "manifest_errors": manifest_errors,
                    "manifest_error_count": len(manifest_errors),
                },
            )
        if isinstance(request, GetExperimentRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            manifest_status = str(manifest.get("status", "partial"))
            response_status = (
                manifest_status
                if manifest_status in {"running", "completed", "failed", "partial", "interrupted"}
                else "partial"
            )
            return BrowserActionResponse(
                operation=request.operation,
                status=response_status,
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={
                    "experiment": self._experiment_summary(manifest),
                    "manifest_relative_path": self._manifest_relative_path(
                        request.payload.experiment_id
                    ),
                },
            )
        if isinstance(request, ListEvidenceRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            evidence = self._evidence_index(manifest)
            if request.payload.kind:
                evidence = [item for item in evidence if item.get("kind") == request.payload.kind]
            evidence = evidence[: request.payload.limit]
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"evidence": evidence, "count": len(evidence)},
            )
        if isinstance(request, GetNetworkEvidenceRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            item = self._find_evidence(manifest, request.payload.evidence_id)
            if item.get("kind") != "network_request":
                raise BrowserServiceError(
                    "evidence_kind_mismatch",
                    "The requested evidence is not network_request evidence.",
                    409,
                )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"evidence": item},
            )
        if isinstance(request, GetRequestShapeRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            item = self._find_evidence(manifest, request.payload.evidence_id)
            if item.get("kind") != "network_request":
                raise BrowserServiceError(
                    "evidence_kind_mismatch",
                    "The requested evidence is not network_request evidence.",
                    409,
                )
            paths = item.get("artifact_paths")
            paths = paths if isinstance(paths, dict) else {}
            shape_relative = paths.get("request_shape")
            redacted_relative = paths.get("request_body_redacted")
            if not isinstance(shape_relative, str):
                raise BrowserServiceError(
                    "request_shape_missing",
                    "The network evidence has no JSON request shape artifact.",
                    404,
                )
            try:
                shape_path = (self.experiments.root / shape_relative).resolve()
                shape_path.relative_to(self.experiments.root)
                shape = json.loads(shape_path.read_text(encoding="utf-8"))
                redacted = None
                if request.payload.include_redacted_body and isinstance(redacted_relative, str):
                    redacted_path = (self.experiments.root / redacted_relative).resolve()
                    redacted_path.relative_to(self.experiments.root)
                    redacted = self._bounded_redacted_subtree(
                        json.loads(redacted_path.read_text(encoding="utf-8")),
                        path_prefix=request.payload.path_prefix,
                        max_depth=request.payload.max_depth,
                        max_array_items=request.payload.max_array_items,
                    )
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise BrowserServiceError(
                    "request_shape_invalid",
                    "The saved request shape artifact is unavailable or invalid.",
                    409,
                ) from exc
            all_paths = shape.get("paths")
            all_paths = all_paths if isinstance(all_paths, dict) else {}
            filtered = self._filter_shape_paths(
                all_paths,
                path_prefix=request.payload.path_prefix,
                max_depth=request.payload.max_depth,
                max_array_items=request.payload.max_array_items,
            )
            start = request.payload.page_idx * request.payload.page_size
            page = filtered[start : start + request.payload.page_size]
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={
                    "evidence_id": request.payload.evidence_id,
                    "request_shape": {
                        "format": shape.get("format", "json-pointer-v1"),
                        "path_prefix": request.payload.path_prefix,
                        "paths": dict(page),
                    },
                    "request_body_redacted": redacted,
                    "pagination": {
                        "page_idx": request.payload.page_idx,
                        "page_size": request.payload.page_size,
                        "total_paths": len(filtered),
                        "has_next_page": start + len(page) < len(filtered),
                    },
                    "limits": {
                        "max_depth": request.payload.max_depth,
                        "max_array_items": request.payload.max_array_items,
                        "include_redacted_body": (request.payload.include_redacted_body),
                    },
                },
            )
        if isinstance(request, GetRequestInitiatorRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            item = self._find_evidence(manifest, request.payload.evidence_id)
            paths = item.get("artifact_paths")
            paths = paths if isinstance(paths, dict) else {}
            relative = paths.get("initiator")
            if not isinstance(relative, str):
                raise BrowserServiceError(
                    "initiator_evidence_missing",
                    "The network evidence has no saved initiator artifact.",
                    404,
                )
            path = (self.experiments.root / relative).resolve()
            try:
                path.relative_to(self.experiments.root)
                initiator = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                raise BrowserServiceError(
                    "initiator_evidence_invalid",
                    "The saved initiator artifact is unavailable or invalid.",
                    409,
                ) from exc
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={
                    "evidence_id": request.payload.evidence_id,
                    "initiator": initiator,
                },
            )
        if isinstance(request, ListConsoleErrorsRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            evidence = [
                item
                for item in self._evidence_index(manifest)
                if item.get("kind") == "console_message"
            ][: request.payload.limit]
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"console_errors": evidence, "count": len(evidence)},
            )
        if isinstance(request, SearchScriptsRequest):

            async def search(deadline: Deadline) -> dict[str, Any]:
                return await self.js_reverse.search_scripts(
                    request.payload.query,
                    deadline,
                    url_filter=request.payload.url_filter,
                    max_results=request.payload.max_results,
                    exclude_minified=request.payload.exclude_minified,
                )

            result = await self._run_aligned_inspection(
                session_id=request.payload.session_id,
                operation=request.operation,
                callback=search,
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=request.payload.session_id,
                result={"search": result},
            )
        if isinstance(request, GetScriptSourceRequest):

            async def source(deadline: Deadline) -> dict[str, Any]:
                return await self.js_reverse.get_script_source(
                    deadline,
                    url=request.payload.url,
                    script_id=request.payload.script_id,
                    start_line=request.payload.start_line,
                    end_line=request.payload.end_line,
                    offset=request.payload.offset,
                    length=request.payload.length,
                )

            result = await self._run_aligned_inspection(
                session_id=request.payload.session_id,
                operation=request.operation,
                callback=source,
            )
            return BrowserActionResponse(
                operation=request.operation,
                status="completed",
                session_id=request.payload.session_id,
                result={"source": result},
            )
        if isinstance(request, GetStreamStatusRequest):
            manifest = self.experiments.load_manifest(request.payload.experiment_id)
            runtime = self._manifest_stream_runtime(manifest)
            expected_uuid = runtime.get("capture_uuid")
            if (
                request.payload.capture_uuid is not None
                and expected_uuid is not None
                and request.payload.capture_uuid != expected_uuid
            ):
                raise BrowserServiceError(
                    "capture_uuid_mismatch",
                    "The supplied capture UUID does not match the experiment manifest.",
                    409,
                )
            owner = self.coordinator.browser_owner
            capture_id = runtime.get("capture_id")
            recorded_generation = runtime.get("transport_generation")
            live = (
                manifest.get("status") == "running"
                and owner is not None
                and owner.experiment_id == request.payload.experiment_id
                and isinstance(capture_id, int)
                and recorded_generation == self._transport_generation()
            )
            if live:
                status = await self.js_reverse.get_stream_status(int(capture_id), Deadline(10_000))
                returned_capture = status.get("capture")
                returned_uuid = (
                    returned_capture.get("captureUuid")
                    if isinstance(returned_capture, dict)
                    else None
                )
                if expected_uuid and returned_uuid != expected_uuid:
                    raise BrowserServiceError(
                        "capture_identity_mismatch",
                        "Live MCP capture identity does not match the experiment manifest.",
                        409,
                        dispatch_started=True,
                    )
                source = "live-mcp"
            else:
                persisted = manifest.get("stream_status")
                status = (
                    dict(persisted)
                    if isinstance(persisted, dict)
                    else {
                        "capture": {
                            "captureUuid": expected_uuid,
                            "relativeDir": runtime.get("capture_relative_dir"),
                            "status": manifest.get("status"),
                        },
                        "requests": [],
                    }
                )
                source = "manifest"
            return BrowserActionResponse(
                operation=request.operation,
                status=("running" if manifest.get("status") == "running" else "completed"),
                session_id=manifest.get("session_id"),
                experiment_id=request.payload.experiment_id,
                result={"stream": status, "source": source},
            )
        raise BrowserServiceError("unsupported_operation", "Unsupported inspect operation", 400)
