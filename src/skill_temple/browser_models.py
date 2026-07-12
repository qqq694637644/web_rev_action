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


class EventPredicate(StrictModel):
    type: Literal[
        "exact_data",
        "event_name",
        "json_path_equals",
        "network_terminal",
        "selector_state",
    ]
    value: Any | None = None
    path: str | None = Field(default=None, max_length=512)
    event_name: str | None = Field(default=None, max_length=256)
    locator: Locator | None = None

    @model_validator(mode="after")
    def validate_predicate(self) -> EventPredicate:
        if self.type == "json_path_equals" and not self.path:
            raise ValueError("json_path_equals requires path")
        if self.type == "event_name" and not self.event_name:
            raise ValueError("event_name requires event_name")
        if self.type == "selector_state":
            if not self.locator:
                raise ValueError("selector_state requires locator")
            if self.value not in {"visible", "hidden"}:
                raise ValueError("selector_state value must be visible or hidden")
        return self


class WaitCondition(StrictModel):
    type: Literal[
        "timeout",
        "selector_visible",
        "selector_hidden",
        "request_observed",
        "response_observed",
        "network_idle",
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


FlowAction = Literal[
    "navigate",
    "reload",
    "click",
    "fill",
    "type",
    "press",
    "select",
    "check",
    "uncheck",
    "hover",
    "upload",
    "wait",
    "assert",
    "snapshot",
]


class FlowStep(StrictModel):
    step_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", min_length=1, max_length=128)
    action: FlowAction
    locator: Locator | None = None
    value: str | None = Field(default=None, max_length=32_000)
    values: list[str] = Field(default_factory=list, max_length=32)
    condition: WaitCondition | None = None
    timeout_ms: int = Field(default=5_000, ge=1, le=1_800_000)
    intent: Literal["stop_generation"] | None = None

    @model_validator(mode="after")
    def validate_step(self) -> FlowStep:
        locator_actions = {"click", "fill", "select", "check", "uncheck", "hover"}
        if self.action in locator_actions and not self.locator:
            raise ValueError(f"{self.action} requires locator")
        if self.action in {"navigate", "fill", "type", "press", "select"} and self.value is None:
            raise ValueError(f"{self.action} requires value")
        if self.action == "upload" and not self.values:
            raise ValueError("upload requires values")
        if self.action in {"wait", "assert"} and not self.condition:
            raise ValueError(f"{self.action} requires condition")
        return self


class BrowserTarget(StrictModel):
    start_url: str | None = Field(default=None, max_length=8192)
    expected_url_contains: str | None = Field(default=None, max_length=4096)
    page_index: int = Field(default=0, ge=0, le=100)


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
    scripts: bool = False


class OpenSessionPayload(StrictModel):
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    browser_endpoint: str | None = Field(default=None, max_length=8192)
    target: BrowserTarget = Field(default_factory=BrowserTarget)
    deadline_ms: int = Field(default=15_000, ge=1_000, le=42_000)


class CloseSessionPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    deadline_ms: int = Field(default=10_000, ge=1_000, le=42_000)


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

    @model_validator(mode="after")
    def validate_stop_sequence(self) -> CaptureFlowPayload:
        stop_indexes = [
            index
            for index, step in enumerate(self.flow)
            if step.intent == "stop_generation"
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
            has_canceled_wait = any(
                step.action in {"wait", "assert"}
                and step.condition is not None
                and step.condition.type == "network_canceled"
                for step in after
            ) or (self.wait_for is not None and self.wait_for.type == "network_canceled")
            if not has_started_stream:
                raise ValueError(
                    "stop_generation requires an earlier first_event or event_predicate wait"
                )
            if not has_canceled_wait:
                raise ValueError(
                    "stop_generation requires a later network_canceled wait for the same experiment"
                )
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


class GetSessionPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)


class ListExperimentsPayload(StrictModel):
    session_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    limit: int = Field(default=50, ge=1, le=200)


class GetExperimentPayload(StrictModel):
    experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)


class GetStreamStatusPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    capture_id: int = Field(gt=0)


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
    | CloseSessionRequest,
    Field(discriminator="operation"),
]


class GetStreamStatusRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["get_stream_status"]
    payload: GetStreamStatusPayload


InspectBrowserEvidenceRequest = Annotated[
    GetSessionRequest
    | ListExperimentsRequest
    | GetExperimentRequest
    | GetStreamStatusRequest,
    Field(discriminator="operation"),
]


class FlowStepResult(StrictModel):
    step_id: str
    status: Literal["completed", "failed", "skipped", "timed_out"]
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
