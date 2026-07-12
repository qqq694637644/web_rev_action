"""Typed public and internal models for browser experiments."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillBinding(StrictModel):
    skill_id: str = Field(min_length=1, max_length=128)
    content_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class Locator(StrictModel):
    ref: str | None = Field(default=None, max_length=256)
    role: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=512)
    label: str | None = Field(default=None, max_length=512)
    placeholder: str | None = Field(default=None, max_length=512)
    test_id: str | None = Field(default=None, max_length=512)
    text: str | None = Field(default=None, max_length=1024)
    css: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validate_strategy(self) -> Locator:
        strategies = [
            self.ref,
            self.role,
            self.label,
            self.placeholder,
            self.test_id,
            self.text,
            self.css,
        ]
        if sum(value is not None for value in strategies) != 1:
            raise ValueError("locator must define exactly one strategy")
        if self.role and not self.name:
            raise ValueError("role locator requires name")
        if not self.role and self.name:
            raise ValueError("name is only valid with role")
        return self


class RequestMatcher(StrictModel):
    url_contains: str | None = Field(default=None, max_length=4096)
    method: str | None = Field(default=None, pattern=r"^[A-Z]+$", max_length=16)
    resource_types: list[str] = Field(default_factory=list, max_length=16)
    mime_types: list[str] = Field(default_factory=list, max_length=16)
    request_id: str | None = Field(default=None, max_length=512)


class ExactDataPredicate(StrictModel):
    type: Literal["exact_data"]
    value: str = Field(max_length=64_000)


class EventNamePredicate(StrictModel):
    type: Literal["event_name"]
    event_name: str = Field(min_length=1, max_length=256)


class JsonPathEqualsPredicate(StrictModel):
    type: Literal["json_path_equals"]
    path: str = Field(pattern=r"^\$\.[A-Za-z0-9_.-]+$", max_length=512)
    value: Any


class NetworkTerminalPredicate(StrictModel):
    type: Literal["network_terminal"]
    value: Literal["finished", "canceled", "failed", "stopped"] | None = None


class SelectorStatePredicate(StrictModel):
    type: Literal["selector_state"]
    locator: Locator
    value: Literal["visible", "hidden"]


EventPredicate = Annotated[
    ExactDataPredicate
    | EventNamePredicate
    | JsonPathEqualsPredicate
    | NetworkTerminalPredicate
    | SelectorStatePredicate,
    Field(discriminator="type"),
]


class WaitCondition(StrictModel):
    type: Literal[
        "timeout",
        "selector_visible",
        "selector_hidden",
        "request_observed",
        "response_observed",
        "request_log_stable",
        "first_event",
        "event_predicate",
        "default_done_marker",
        "network_finished",
        "network_canceled",
        "failed",
        "page_url",
    ]
    timeout_ms: int = Field(default=10_000, ge=1, le=1_800_000)
    locator: Locator | None = None
    request_matcher: RequestMatcher | None = None
    predicate: EventPredicate | None = None
    value: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def validate_condition(self) -> WaitCondition:
        if self.type in {"selector_visible", "selector_hidden"} and not self.locator:
            raise ValueError(f"{self.type} requires locator")
        if self.type in {
            "request_observed",
            "response_observed",
            "first_event",
            "event_predicate",
            "default_done_marker",
            "network_finished",
            "network_canceled",
            "failed",
        } and not self.request_matcher:
            raise ValueError(f"{self.type} requires request_matcher")
        if self.type == "event_predicate" and not self.predicate:
            raise ValueError("event_predicate requires predicate")
        if self.type == "page_url" and not self.value:
            raise ValueError("page_url requires value")
        return self


class FlowStepBase(StrictModel):
    step_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", min_length=1, max_length=128)
    timeout_ms: int = Field(default=5_000, ge=1, le=1_800_000)


class NavigateStep(FlowStepBase):
    action: Literal["navigate"]
    value: str = Field(min_length=1, max_length=8192)


class ReloadStep(FlowStepBase):
    action: Literal["reload"]


class ClickStep(FlowStepBase):
    action: Literal["click"]
    locator: Locator
    intent: Literal["stop_generation"] | None = None


class FillStep(FlowStepBase):
    action: Literal["fill"]
    locator: Locator
    value: str = Field(max_length=32_000)


class TypeStep(FlowStepBase):
    action: Literal["type"]
    value: str = Field(max_length=32_000)


class PressStep(FlowStepBase):
    action: Literal["press"]
    value: str = Field(min_length=1, max_length=256)


class SelectStep(FlowStepBase):
    action: Literal["select"]
    locator: Locator
    value: str = Field(max_length=32_000)


class CheckStep(FlowStepBase):
    action: Literal["check"]
    locator: Locator


class UncheckStep(FlowStepBase):
    action: Literal["uncheck"]
    locator: Locator


class HoverStep(FlowStepBase):
    action: Literal["hover"]
    locator: Locator


class UploadStep(FlowStepBase):
    action: Literal["upload"]
    locator: Locator | None = None
    values: list[str] = Field(min_length=1, max_length=32)


class WaitStep(FlowStepBase):
    action: Literal["wait"]
    condition: WaitCondition


class AssertStep(FlowStepBase):
    action: Literal["assert"]
    condition: WaitCondition


class SnapshotStep(FlowStepBase):
    action: Literal["snapshot"]


FlowStep = Annotated[
    NavigateStep
    | ReloadStep
    | ClickStep
    | FillStep
    | TypeStep
    | PressStep
    | SelectStep
    | CheckStep
    | UncheckStep
    | HoverStep
    | UploadStep
    | WaitStep
    | AssertStep
    | SnapshotStep,
    Field(discriminator="action"),
]


class BrowserTarget(StrictModel):
    start_url: str | None = Field(default=None, max_length=8192)
    expected_url_contains: str | None = Field(default=None, max_length=4096)
    page_index: int | None = Field(default=None, ge=0, le=100)


class PrimaryRequest(StrictModel):
    url_contains: str | None = Field(default=None, max_length=4096)
    method: str | None = Field(default=None, pattern=r"^[A-Z]+$", max_length=16)
    resource_types: list[str] = Field(default_factory=list, max_length=16)
    mime_types: list[str] = Field(
        default_factory=lambda: ["text/event-stream"],
        min_length=1,
        max_length=16,
    )
    expected_min_matches: int = Field(default=1, ge=0, le=100)
    expected_max_matches: int = Field(default=1, ge=1, le=100)
    allow_supporting_failures: bool = True
    include_in_flight: bool = False

    @model_validator(mode="after")
    def validate_match_count(self) -> PrimaryRequest:
        if self.expected_max_matches < self.expected_min_matches:
            raise ValueError("expected_max_matches must be >= expected_min_matches")
        return self


class CaptureOptions(StrictModel):
    network: bool = True
    stream: bool = True
    trace: bool = True
    screenshots: bool = True
    page_snapshots: bool = True
    console_errors: bool = True


NetworkExportPart = Literal[
    "all",
    "responseHeaders",
    "responseBody",
    "requestBody",
    "queryParams",
]


class NetworkEvidenceSelector(StrictModel):
    selector_id: str = Field(
        pattern=r"^[a-zA-Z0-9_.-]+$", min_length=1, max_length=128
    )
    matcher: RequestMatcher = Field(default_factory=RequestMatcher)
    max_matches: int = Field(default=5, ge=1, le=50)
    export_parts: list[NetworkExportPart] = Field(
        default_factory=lambda: ["all"], min_length=1, max_length=5
    )
    include_initiator: bool = True
    include_cookie_provenance: bool = False
    cookie_names: list[str] = Field(default_factory=list, max_length=20)


class ExperimentSeries(StrictModel):
    analysis_series_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128
    )
    scenario_type: str | None = Field(default=None, max_length=128)
    predecessor_experiment_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128
    )
    sequence_index: int | None = Field(default=None, ge=0, le=100_000)
    conversation_key: str | None = Field(default=None, max_length=512)


class ObjectiveRequirements(StrictModel):
    require_raw_capture: bool = True
    require_semantic_parse: bool = False
    require_request_snapshot: bool = False
    require_artifacts: bool = True


class OpenSessionPayload(StrictModel):
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    browser_endpoint: str | None = Field(default=None, max_length=8192)
    target: BrowserTarget = Field(default_factory=BrowserTarget)
    deadline_ms: int = Field(default=15_000, ge=1_000, le=42_000)


class CloseSessionPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    deadline_ms: int = Field(default=10_000, ge=1_000, le=42_000)


class CancelExperimentPayload(StrictModel):
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)


class CaptureFlowPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    objective: str = Field(min_length=1, max_length=2048)
    target: BrowserTarget = Field(default_factory=BrowserTarget)
    primary_request: PrimaryRequest = Field(default_factory=PrimaryRequest)
    flow: list[FlowStep] = Field(default_factory=list, max_length=100)
    wait_for: WaitCondition | None = None
    execution_mode: Literal["job", "sync"] = "job"
    deadline_ms: int = Field(default=42_000, ge=1_000, le=42_000)
    job_timeout_ms: int = Field(default=300_000, ge=10_000, le=1_800_000)
    capture: CaptureOptions = Field(default_factory=CaptureOptions)
    requirements: ObjectiveRequirements = Field(default_factory=ObjectiveRequirements)
    network_evidence: list[NetworkEvidenceSelector] = Field(
        default_factory=list, max_length=20
    )
    series: ExperimentSeries = Field(default_factory=ExperimentSeries)

    @model_validator(mode="after")
    def validate_stop_sequence(self) -> CaptureFlowPayload:
        if self.target.start_url is not None:
            raise ValueError(
                "capture target.start_url is not allowed; add an explicit navigate flow step "
                "so Trace and stream capture start before navigation"
            )
        stop_indexes = [
            index
            for index, step in enumerate(self.flow)
            if getattr(step, "intent", None) == "stop_generation"
        ]
        for stop_index in stop_indexes:
            before = self.flow[:stop_index]
            after = self.flow[stop_index + 1 :]
            has_started_stream = any(
                step.action in {"wait", "assert"}
                and step.condition is not None
                and step.condition.type in {"first_event", "event_predicate"}
                for step in before
            )
            has_terminal_observation = any(
                step.action in {"wait", "assert"}
                and step.condition is not None
                and step.condition.type
                in {
                    "network_canceled",
                    "network_finished",
                    "event_predicate",
                    "request_observed",
                    "response_observed",
                    "selector_visible",
                    "selector_hidden",
                    "timeout",
                    "failed",
                }
                for step in after
            ) or (
                self.wait_for is not None
                and self.wait_for.type
                in {
                    "network_canceled",
                    "network_finished",
                    "event_predicate",
                    "request_observed",
                    "response_observed",
                    "selector_visible",
                    "selector_hidden",
                    "timeout",
                    "failed",
                }
            )
            if not has_started_stream:
                raise ValueError(
                    "stop_generation requires an earlier first_event or event_predicate wait"
                )
            if not has_terminal_observation:
                raise ValueError(
                    "stop_generation requires a later network, event, selector, "
                    "or timeout observation"
                )
        return self


class RemoveJsonPathMutation(StrictModel):
    type: Literal["remove_json_path"]
    path: str = Field(pattern=r"^\$\.[A-Za-z0-9_.-]+$", max_length=512)


class ReplaceJsonPathMutation(StrictModel):
    type: Literal["replace_json_path"]
    path: str = Field(pattern=r"^\$\.[A-Za-z0-9_.-]+$", max_length=512)
    value: Any


class RemoveHeaderMutation(StrictModel):
    type: Literal["remove_header"]
    name: str = Field(min_length=1, max_length=256)


class ReplaceHeaderMutation(StrictModel):
    type: Literal["replace_header"]
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=32_000)


class RemoveQueryParameterMutation(StrictModel):
    type: Literal["remove_query_parameter"]
    name: str = Field(min_length=1, max_length=512)


class ReplaceQueryParameterMutation(StrictModel):
    type: Literal["replace_query_parameter"]
    name: str = Field(min_length=1, max_length=512)
    value: str = Field(max_length=32_000)


ReplayMutation = Annotated[
    RemoveJsonPathMutation
    | ReplaceJsonPathMutation
    | RemoveHeaderMutation
    | ReplaceHeaderMutation
    | RemoveQueryParameterMutation
    | ReplaceQueryParameterMutation,
    Field(discriminator="type"),
]


class ReplayRequestPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    objective: str = Field(min_length=1, max_length=2048)
    source_experiment_id: str = Field(
        pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128
    )
    source_evidence_id: str = Field(
        pattern=r"^[a-zA-Z0-9_.-]+$", max_length=256
    )
    mode: Literal["browser_context"] = "browser_context"
    mutations: list[ReplayMutation] = Field(default_factory=list, max_length=64)
    target: BrowserTarget = Field(default_factory=BrowserTarget)
    wait_for: WaitCondition | None = None
    execution_mode: Literal["job", "sync"] = "job"
    deadline_ms: int = Field(default=42_000, ge=1_000, le=42_000)
    job_timeout_ms: int = Field(default=300_000, ge=10_000, le=1_800_000)
    capture: CaptureOptions = Field(
        default_factory=lambda: CaptureOptions(stream=False)
    )
    requirements: ObjectiveRequirements = Field(
        default_factory=lambda: ObjectiveRequirements(
            require_raw_capture=False,
            require_request_snapshot=True,
            require_artifacts=True,
        )
    )
    network_evidence: list[NetworkEvidenceSelector] = Field(
        default_factory=list, max_length=20
    )
    series: ExperimentSeries = Field(default_factory=ExperimentSeries)

    @model_validator(mode="after")
    def validate_replay(self) -> ReplayRequestPayload:
        if self.target.start_url is not None:
            raise ValueError("replay_request does not allow target.start_url")
        return self


class CaptureBaselinePayload(CaptureFlowPayload):
    objective: str = "capture baseline page and network state"
    primary_request: PrimaryRequest = Field(
        default_factory=lambda: PrimaryRequest(
            expected_min_matches=0,
            expected_max_matches=100,
        )
    )
    flow: list[FlowStep] = Field(default_factory=list, max_length=0)


class OpenSessionRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["open_session"]
    payload: OpenSessionPayload
    skill_binding: SkillBinding | None = None


class CaptureBaselineRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["capture_baseline"]
    payload: CaptureBaselinePayload
    skill_binding: SkillBinding | None = None


class CaptureFlowRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["capture_flow"]
    payload: CaptureFlowPayload
    skill_binding: SkillBinding | None = None


class CloseSessionRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["close_session"]
    payload: CloseSessionPayload
    skill_binding: SkillBinding | None = None


class CancelExperimentRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["cancel_experiment"]
    payload: CancelExperimentPayload
    skill_binding: SkillBinding | None = None


class ReplayRequestRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["replay_request"]
    payload: ReplayRequestPayload
    skill_binding: SkillBinding | None = None


class GetSessionPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)


class ListExperimentsPayload(StrictModel):
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    limit: int = Field(default=50, ge=1, le=200)


class GetExperimentPayload(StrictModel):
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)


class GetStreamStatusPayload(StrictModel):
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    capture_uuid: str | None = Field(default=None, max_length=128)


class ListEvidencePayload(StrictModel):
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    kind: str | None = Field(default=None, max_length=128)
    limit: int = Field(default=100, ge=1, le=500)


class GetNetworkEvidencePayload(StrictModel):
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    evidence_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=256)


class GetRequestInitiatorPayload(GetNetworkEvidencePayload):
    pass


class SearchScriptsPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    query: str = Field(min_length=1, max_length=4096)
    url_filter: str | None = Field(default=None, max_length=4096)
    max_results: int = Field(default=30, ge=1, le=100)
    exclude_minified: bool = False


class GetScriptSourcePayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    url: str | None = Field(default=None, max_length=8192)
    script_id: str | None = Field(default=None, max_length=512)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    offset: int | None = Field(default=None, ge=0)
    length: int | None = Field(default=None, ge=1, le=200_000)

    @model_validator(mode="after")
    def validate_selector(self) -> GetScriptSourcePayload:
        if bool(self.url) == bool(self.script_id):
            raise ValueError("provide exactly one of url or script_id")
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must be provided together")
        if (self.offset is None) != (self.length is None):
            raise ValueError("offset and length must be provided together")
        if self.start_line is not None and self.offset is not None:
            raise ValueError("line range and offset range are mutually exclusive")
        return self


class ListConsoleErrorsPayload(StrictModel):
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    limit: int = Field(default=100, ge=1, le=500)


class GetSessionRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["get_session"]
    payload: GetSessionPayload


class ListExperimentsRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["list_experiments"]
    payload: ListExperimentsPayload = Field(default_factory=ListExperimentsPayload)


class GetExperimentRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["get_experiment"]
    payload: GetExperimentPayload


RunBrowserExperimentRequest = Annotated[
    OpenSessionRequest
    | CaptureBaselineRequest
    | CaptureFlowRequest
    | CloseSessionRequest
    | CancelExperimentRequest
    | ReplayRequestRequest,
    Field(discriminator="operation"),
]


class GetStreamStatusRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["get_stream_status"]
    payload: GetStreamStatusPayload


class ListEvidenceRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["list_evidence"]
    payload: ListEvidencePayload


class GetNetworkEvidenceRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["get_network_evidence"]
    payload: GetNetworkEvidencePayload


class GetRequestInitiatorRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["get_request_initiator"]
    payload: GetRequestInitiatorPayload


class SearchScriptsRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["search_scripts"]
    payload: SearchScriptsPayload


class GetScriptSourceRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["get_script_source"]
    payload: GetScriptSourcePayload


class ListConsoleErrorsRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["list_console_errors"]
    payload: ListConsoleErrorsPayload


InspectBrowserEvidenceRequest = Annotated[
    GetSessionRequest
    | ListExperimentsRequest
    | GetExperimentRequest
    | GetStreamStatusRequest
    | ListEvidenceRequest
    | GetNetworkEvidenceRequest
    | GetRequestInitiatorRequest
    | SearchScriptsRequest
    | GetScriptSourceRequest
    | ListConsoleErrorsRequest,
    Field(discriminator="operation"),
]


class FlowStepResult(StrictModel):
    step_id: str
    status: Literal[
        "completed",
        "failed",
        "skipped",
        "timed_out",
        "canceled",
        "canceled_outcome_unknown",
    ]
    started_at: str
    ended_at: str
    snapshot_ref: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class BrowserActionResponse(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: str
    status: Literal["running", "completed", "failed", "partial", "interrupted"]
    session_id: str | None = None
    experiment_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
