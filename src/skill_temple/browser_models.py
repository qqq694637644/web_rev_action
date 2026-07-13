"""Typed public and internal models for browser experiments."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_BROWSER_MANAGED_HEADERS = {
    "accept-encoding",
    "connection",
    "content-length",
    "cookie",
    "host",
    "origin",
    "proxy-authorization",
    "referer",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def _validate_json_pointer(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("JSON mutation path must be a JSON Pointer starting with '/'")
    if path == "/":
        raise ValueError("JSON mutation cannot replace or remove the document root")
    for token in path.split("/")[1:]:
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                    raise ValueError("JSON Pointer escape must be ~0 or ~1")
                index += 2
                continue
            if token[index] in {"*", "[", "]"}:
                raise ValueError("JSON Pointer wildcards and bracket expressions are not allowed")
            index += 1
    return path


def _validate_mutable_header(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in _BROWSER_MANAGED_HEADERS or normalized.startswith("sec-"):
        raise ValueError(
            f"Header '{name}' is browser-managed and cannot be mutated by browser_context replay"
        )
    return name


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
        if (
            self.type
            in {
                "request_observed",
                "response_observed",
                "first_event",
                "event_predicate",
                "default_done_marker",
                "network_finished",
                "network_canceled",
                "failed",
            }
            and not self.request_matcher
        ):
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
    mime_types: list[str] = Field(default_factory=list, max_length=16)
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
    selector_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", min_length=1, max_length=128)
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
    network_evidence: list[NetworkEvidenceSelector] = Field(default_factory=list, max_length=20)
    series: ExperimentSeries = Field(default_factory=ExperimentSeries)

    @model_validator(mode="after")
    def validate_capture_target(self) -> CaptureFlowPayload:
        if self.target.start_url is not None:
            raise ValueError(
                "capture target.start_url is not allowed; add an explicit navigate flow step "
                "so Trace and stream capture start before navigation"
            )
        return self


class RemoveJsonPathMutation(StrictModel):
    type: Literal["remove_json_path"]
    path: str = Field(min_length=2, max_length=512)

    @model_validator(mode="after")
    def validate_path(self) -> RemoveJsonPathMutation:
        _validate_json_pointer(self.path)
        return self


class AddJsonPathMutation(StrictModel):
    type: Literal["add_json_path"]
    path: str = Field(min_length=2, max_length=512)
    value: Any

    @model_validator(mode="after")
    def validate_path(self) -> AddJsonPathMutation:
        _validate_json_pointer(self.path)
        return self


class ReplaceJsonPathMutation(StrictModel):
    type: Literal["replace_json_path"]
    path: str = Field(min_length=2, max_length=512)
    value: Any

    @model_validator(mode="after")
    def validate_path(self) -> ReplaceJsonPathMutation:
        _validate_json_pointer(self.path)
        return self


class RemoveHeaderMutation(StrictModel):
    type: Literal["remove_header"]
    name: str = Field(min_length=1, max_length=256)
    occurrence: int | Literal["all"] = Field(default="all")

    @model_validator(mode="after")
    def validate_header(self) -> RemoveHeaderMutation:
        _validate_mutable_header(self.name)
        return self


class ReplaceHeaderMutation(StrictModel):
    type: Literal["replace_header"]
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=32_000)
    occurrence: int | Literal["all"] = Field(default="all")

    @model_validator(mode="after")
    def validate_header(self) -> ReplaceHeaderMutation:
        _validate_mutable_header(self.name)
        return self


class AddHeaderMutation(StrictModel):
    type: Literal["add_header"]
    name: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=32_000)
    occurrence: int | Literal["append"] = Field(default="append")

    @model_validator(mode="after")
    def validate_header(self) -> AddHeaderMutation:
        _validate_mutable_header(self.name)
        return self


class RemoveQueryParameterMutation(StrictModel):
    type: Literal["remove_query_parameter"]
    name: str = Field(min_length=1, max_length=512)
    occurrence: int | Literal["all"] = Field(default="all")


class ReplaceQueryParameterMutation(StrictModel):
    type: Literal["replace_query_parameter"]
    name: str = Field(min_length=1, max_length=512)
    value: str = Field(max_length=32_000)
    occurrence: int | Literal["all"] = Field(default="all")


class AddQueryParameterMutation(StrictModel):
    type: Literal["add_query_parameter"]
    name: str = Field(min_length=1, max_length=512)
    value: str = Field(max_length=32_000)
    occurrence: int | Literal["append"] = Field(default="append")


ReplayMutation = Annotated[
    RemoveJsonPathMutation
    | AddJsonPathMutation
    | ReplaceJsonPathMutation
    | RemoveHeaderMutation
    | ReplaceHeaderMutation
    | AddHeaderMutation
    | RemoveQueryParameterMutation
    | ReplaceQueryParameterMutation
    | AddQueryParameterMutation,
    Field(discriminator="type"),
]


class VolatileBinding(StrictModel):
    binding_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", min_length=1, max_length=128)
    target: Literal["json_pointer", "header", "query_parameter"]
    path: str | None = Field(default=None, max_length=512)
    name: str | None = Field(default=None, max_length=256)
    occurrence: int = Field(default=0, ge=0, le=255)
    value_source: Literal["generated", "preserve_source", "setup_output"] = "generated"
    generator: (
        Literal[
            "uuid4",
            "timestamp_ms",
            "timestamp_iso",
            "random_hex_16",
        ]
        | None
    ) = None
    reuse_policy: Literal["fresh_equivalent", "same_value"] = "fresh_equivalent"

    @model_validator(mode="after")
    def validate_target(self) -> VolatileBinding:
        if self.target == "json_pointer":
            if not self.path or self.name is not None:
                raise ValueError("json_pointer binding requires path and forbids name")
            _validate_json_pointer(self.path)
        else:
            if not self.name or self.path is not None:
                raise ValueError(f"{self.target} binding requires name and forbids path")
            if self.target == "header":
                _validate_mutable_header(self.name)
        if self.value_source == "generated" and self.generator is None:
            raise ValueError("generated volatile binding requires generator")
        if self.value_source == "preserve_source":
            if self.generator is not None:
                raise ValueError("preserve_source binding must not declare generator")
            if self.reuse_policy != "same_value":
                raise ValueError("preserve_source binding requires reuse_policy=same_value")
        if self.value_source == "setup_output" and self.generator is not None:
            raise ValueError("setup_output binding must not declare generator")
        return self


class NetworkResponseJsonSetupOutput(StrictModel):
    binding_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", min_length=1, max_length=128)
    source: Literal["network_response_json"]
    selector: RequestMatcher
    pointer: str = Field(min_length=2, max_length=512)
    occurrence: int | Literal["first", "last"] = "last"

    @model_validator(mode="after")
    def validate_pointer(self) -> NetworkResponseJsonSetupOutput:
        _validate_json_pointer(self.pointer)
        return self


SetupOutput = Annotated[
    NetworkResponseJsonSetupOutput,
    Field(discriminator="source"),
]


class EnvironmentComparisonPolicy(StrictModel):
    required_dimensions: list[
        Literal[
            "page_id",
            "page_url",
            "page_origin",
            "request_origin",
            "request_path",
            "request_context_sha256",
            "conversation_current_node",
            "critical_bundle_sha256",
        ]
    ] = Field(
        default_factory=lambda: ["page_origin", "request_context_sha256"],
        max_length=8,
    )
    advisory_dimensions: list[
        Literal[
            "page_id",
            "page_url",
            "page_origin",
            "request_origin",
            "request_path",
            "request_context_sha256",
            "conversation_current_node",
            "critical_bundle_sha256",
        ]
    ] = Field(
        default_factory=lambda: [
            "page_url",
            "request_origin",
            "request_path",
            "conversation_current_node",
            "critical_bundle_sha256",
        ],
        max_length=8,
    )
    context_header_names: list[str] = Field(
        default_factory=lambda: [
            "authorization",
            "proxy-authorization",
            "x-csrf-token",
            "x-xsrf-token",
        ],
        max_length=64,
    )

    @model_validator(mode="after")
    def normalize_policy(self) -> EnvironmentComparisonPolicy:
        self.required_dimensions = list(dict.fromkeys(self.required_dimensions))
        self.advisory_dimensions = [
            item
            for item in dict.fromkeys(self.advisory_dimensions)
            if item not in self.required_dimensions
        ]
        self.context_header_names = sorted(
            {item.strip().lower() for item in self.context_header_names if item.strip()}
        )
        return self


class ReplayTransportOptions(StrictModel):
    credentials: Literal["omit", "same-origin", "include"] = "include"
    redirect: Literal["follow", "error", "manual"] = "follow"
    cache: Literal[
        "default",
        "no-store",
        "reload",
        "no-cache",
        "force-cache",
        "only-if-cached",
    ] = "default"
    referrer_policy: Literal[
        "",
        "no-referrer",
        "no-referrer-when-downgrade",
        "origin",
        "origin-when-cross-origin",
        "same-origin",
        "strict-origin",
        "strict-origin-when-cross-origin",
        "unsafe-url",
    ] = ""
    keepalive: bool = False
    mode: Literal["cors", "no-cors", "same-origin"] = "cors"
    priority: Literal["high", "low", "auto"] = "auto"


class ReplayTerminalCondition(StrictModel):
    type: Literal[
        "exact_sse_data",
        "byte_pattern",
        "network_close",
        "idle_window",
    ]
    value: str | None = Field(default=None, max_length=4096)
    event_name: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_terminal_condition(self) -> ReplayTerminalCondition:
        if self.type in {"exact_sse_data", "byte_pattern"} and self.value is None:
            raise ValueError(f"{self.type} requires value")
        if self.type != "exact_sse_data" and self.event_name is not None:
            raise ValueError("event_name is only valid for exact_sse_data")
        return self


class ReplayControlPayload(StrictModel):
    session_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    objective: str = Field(min_length=1, max_length=2048)
    source_experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    source_evidence_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=256)
    mode: Literal["browser_context"] = "browser_context"
    replay_mode: Literal["control"]
    mutations: list[ReplayMutation] = Field(default_factory=list, max_length=0)
    volatile_bindings: list[VolatileBinding] = Field(default_factory=list, max_length=32)
    target: BrowserTarget = Field(default_factory=BrowserTarget)
    setup_flow: list[FlowStep] = Field(default_factory=list, max_length=20)
    setup_outputs: list[SetupOutput] = Field(default_factory=list, max_length=32)
    wait_for: WaitCondition | None = None
    verification_flow: list[FlowStep] = Field(default_factory=list, max_length=20)
    execution_mode: Literal["job", "sync"] = "job"
    deadline_ms: int = Field(default=42_000, ge=1_000, le=42_000)
    job_timeout_ms: int = Field(default=300_000, ge=10_000, le=1_800_000)
    max_response_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=8_192,
        le=64 * 1024 * 1024,
    )
    stream_idle_timeout_ms: int = Field(default=15_000, ge=1_000, le=120_000)
    response_mode: Literal["auto", "ordinary", "sse", "ndjson", "raw_stream"] = "auto"
    terminal_conditions: list[ReplayTerminalCondition] = Field(
        default_factory=lambda: [ReplayTerminalCondition(type="network_close")],
        max_length=8,
    )
    default_done_marker: str | None = Field(default=None, max_length=512)
    default_done_event_name: str | None = Field(default=None, max_length=128)
    raw_only: bool = False
    ignored_cookie_names: list[str] = Field(default_factory=list, max_length=64)
    ignored_context_headers: list[str] = Field(default_factory=list, max_length=64)
    normalize_wire_order: bool = False
    environment_comparison: EnvironmentComparisonPolicy = Field(
        default_factory=EnvironmentComparisonPolicy
    )
    transport: ReplayTransportOptions = Field(default_factory=ReplayTransportOptions)
    capture: CaptureOptions = Field(default_factory=lambda: CaptureOptions(stream=False))
    requirements: ObjectiveRequirements = Field(
        default_factory=lambda: ObjectiveRequirements(
            require_raw_capture=False,
            require_request_snapshot=True,
            require_artifacts=True,
        )
    )
    network_evidence: list[NetworkEvidenceSelector] = Field(default_factory=list, max_length=20)
    series: ExperimentSeries = Field(default_factory=ExperimentSeries)

    @model_validator(mode="after")
    def validate_replay(self) -> ReplayControlPayload:
        if self.target.start_url is not None:
            raise ValueError("replay_request does not allow target.start_url")
        if self.replay_mode == "control" and self.mutations:
            raise ValueError("control replay requires mutations=[]")
        step_ids = [step.step_id for step in [*self.setup_flow, *self.verification_flow]]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("setup_flow and verification_flow step_id values must be unique")
        output_ids = [item.binding_id for item in self.setup_outputs]
        if len(output_ids) != len(set(output_ids)):
            raise ValueError("setup_outputs binding_id values must be unique")
        if self.setup_outputs and not self.setup_flow:
            raise ValueError("setup_outputs requires a non-empty setup_flow")
        declared_setup_bindings = {
            item.binding_id
            for item in self.volatile_bindings
            if item.value_source == "setup_output"
        }
        if declared_setup_bindings != set(output_ids):
            raise ValueError(
                "setup_output volatile bindings and setup_outputs must declare the same binding IDs"
            )
        self.ignored_cookie_names = sorted(
            {item.strip().lower() for item in self.ignored_cookie_names if item.strip()}
        )
        self.ignored_context_headers = sorted(
            {item.strip().lower() for item in self.ignored_context_headers if item.strip()}
        )
        return self


class ReplayExploratoryPayload(ReplayControlPayload):
    replay_mode: Literal["exploratory"]
    mutations: list[ReplayMutation] = Field(default_factory=list, max_length=32)


class ReplayTreatmentPayload(StrictModel):
    replay_mode: Literal["treatment"]
    control_experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    mutation: ReplayMutation


ReplayRequestPayload = Annotated[
    ReplayControlPayload | ReplayExploratoryPayload | ReplayTreatmentPayload,
    Field(discriminator="replay_mode"),
]


class OpenSessionRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["open_session"]
    payload: OpenSessionPayload
    skill_binding: SkillBinding | None = None


class CaptureBaselineRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["capture_baseline"]
    payload: CaptureFlowPayload
    skill_binding: SkillBinding | None = None

    @model_validator(mode="before")
    @classmethod
    def apply_capture_flow_preset(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        raw_payload = normalized.get("payload")
        if not isinstance(raw_payload, dict):
            return value
        payload = dict(raw_payload)
        flow = payload.get("flow", [])
        if flow:
            raise ValueError("capture_baseline alias requires an empty flow")
        payload.setdefault("objective", "capture baseline page and network state")
        payload.setdefault(
            "primary_request",
            {
                "expected_min_matches": 0,
                "expected_max_matches": 100,
            },
        )
        payload["flow"] = []
        normalized["payload"] = payload
        return normalized


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


class SaveScriptSourceRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["save_script_source"]
    payload: SaveScriptSourcePayload
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


class GetRequestShapePayload(GetNetworkEvidencePayload):
    path_prefix: str = Field(default="/", min_length=1, max_length=512)
    page_idx: int = Field(default=0, ge=0, le=100_000)
    page_size: int = Field(default=100, ge=1, le=500)
    max_depth: int = Field(default=6, ge=0, le=32)
    max_array_items: int = Field(default=20, ge=1, le=200)
    include_redacted_body: bool = False

    @model_validator(mode="after")
    def validate_prefix(self) -> GetRequestShapePayload:
        if self.path_prefix != "/":
            _validate_json_pointer(self.path_prefix)
        return self


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


class SaveScriptSourcePayload(GetScriptSourcePayload):
    target_experiment_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", max_length=128)
    initiator_evidence_id: str | None = Field(
        default=None, pattern=r"^[a-zA-Z0-9_.-]+$", max_length=256
    )
    evidence_label: str | None = Field(default=None, max_length=128)


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
    | ReplayRequestRequest
    | SaveScriptSourceRequest,
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


class GetRequestShapeRequest(StrictModel):
    contract_version: Literal["1.0"] = "1.0"
    operation: Literal["get_request_shape"]
    payload: GetRequestShapePayload


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
    | GetRequestShapeRequest
    | GetRequestInitiatorRequest
    | SearchScriptsRequest
    | GetScriptSourceRequest
    | ListConsoleErrorsRequest,
    Field(discriminator="operation"),
]


class FlowStepResult(StrictModel):
    step_id: str
    phase: Literal["setup", "action", "verification", "replay"]
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
