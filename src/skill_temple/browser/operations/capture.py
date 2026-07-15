"""Capture responsibility extracted from BrowserActionService."""

# ruff: noqa: F403,F405,I001

from __future__ import annotations

from ._support import *  # noqa: F403

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
                cancelled_error is None
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
            if cancelled_error is None and payload.capture.console_errors:
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
            mutation_assessment: dict[str, Any] | None = None
            response_analysis: dict[str, Any] | None = None
            response_analysis_summary: dict[str, Any] | None = None
            stream_response_contract: dict[str, Any] | None = None
            response_evidence_source: str | None = None
            replay_network_evidence_id: str | None = None
            wire_snapshot: dict[str, Any] | None = None
            pre_dispatch_environment: dict[str, Any] | None = None
            post_response_environment: dict[str, Any] | None = None
            post_verification_environment: dict[str, Any] | None = None
            comparison_results: list[dict[str, Any]] = []
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
            if replay_plan is not None:
                current_stream_facts, current_stream_status = (
                    self._current_replay_stream_summary(
                        [
                            item
                            for item in network_observations
                            if isinstance(item, dict)
                        ],
                        replay_network_evidence_id,
                    )
                )
                comparison_results = self._build_replay_comparison_results(
                    replay_plan,
                    current_request_body_sha256=request_body_canonical_sha256_from_snapshot(
                        wire_snapshot
                    )
                    if isinstance(wire_snapshot, dict)
                    else None,
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
                    "post_flow_alignment": asdict(post_alignment),
                    "capture_health": capture_health,
                    "network_checkpoint": network_checkpoint_value,
                    "network_summary": network_payload,
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
                operation=request.operation,
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
