"""Single source of truth for Browser operation contracts and dispatch metadata."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from ..browser_models import (
    CancelExperimentPayload,
    CancelExperimentRequest,
    CaptureFlowPayload,
    CaptureFlowRequest,
    CloseSessionPayload,
    CloseSessionRequest,
    GetExperimentPayload,
    GetExperimentRequest,
    GetNetworkEvidencePayload,
    GetNetworkEvidenceRequest,
    GetRequestInitiatorPayload,
    GetRequestInitiatorRequest,
    GetRequestShapePayload,
    GetRequestShapeRequest,
    GetScriptSourcePayload,
    GetScriptSourceRequest,
    GetSessionPayload,
    GetSessionRequest,
    GetStreamStatusPayload,
    GetStreamStatusRequest,
    ListConsoleErrorsPayload,
    ListConsoleErrorsRequest,
    ListEvidencePayload,
    ListEvidenceRequest,
    ListExperimentsPayload,
    ListExperimentsRequest,
    OpenSessionPayload,
    OpenSessionRequest,
    ReplayRequestPayload,
    ReplayRequestRequest,
    SaveScriptSourcePayload,
    SaveScriptSourceRequest,
    SearchScriptsPayload,
    SearchScriptsRequest,
)

ActionKind = Literal["run", "inspect"]


@dataclass(frozen=True)
class OperationSpec:
    """Authoritative structural metadata for one Browser operation."""

    name: str
    action: ActionKind
    request_model: type[BaseModel]
    payload_model: type[BaseModel]
    handler_name: str
    consequential: bool
    contract_doc_path: str

    def generated_contract(self) -> dict[str, object]:
        """Return canonical structural contract data used for docs, tests, and hashing."""

        return {
            "operation": self.name,
            "action": self.action,
            "request_model": self.request_model.__name__,
            "payload_model": self.payload_model.__name__,
            "handler_name": self.handler_name,
            "consequential": self.consequential,
            "contract_doc_path": self.contract_doc_path,
            "payload_schema": self.payload_model.model_json_schema(mode="validation"),
        }

    @property
    def contract_hash(self) -> str:
        canonical = json.dumps(
            self.generated_contract(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


class OperationRegistry:
    """Immutable indexed operation registry with consistency checks."""

    def __init__(self, specs: tuple[OperationSpec, ...]) -> None:
        by_name: dict[str, OperationSpec] = {}
        for spec in specs:
            if spec.name in by_name:
                raise ValueError(f"Duplicate Browser operation: {spec.name}")
            if spec.consequential != (spec.action == "run"):
                raise ValueError(
                    f"Operation {spec.name} has inconsistent action/consequential metadata"
                )
            by_name[spec.name] = spec
        self._specs = tuple(sorted(specs, key=lambda item: item.name))
        self._by_name = by_name

    def get(self, operation: str) -> OperationSpec | None:
        return self._by_name.get(operation)

    def require(self, operation: str) -> OperationSpec:
        try:
            return self._by_name[operation]
        except KeyError as exc:
            raise KeyError(f"Unknown Browser operation: {operation}") from exc

    def operations(self, action: ActionKind | None = None) -> tuple[str, ...]:
        return tuple(
            spec.name
            for spec in self._specs
            if action is None or spec.action == action
        )

    def specs(self, action: ActionKind | None = None) -> tuple[OperationSpec, ...]:
        return tuple(
            spec
            for spec in self._specs
            if action is None or spec.action == action
        )

    def contract_hash(self, operation: str) -> str:
        return self.require(operation).contract_hash

    def generated_catalog(self) -> dict[str, object]:
        return {
            "format": "browser-operation-registry-v1",
            "operations": [
                {
                    **spec.generated_contract(),
                    "operation_contract_hash": spec.contract_hash,
                }
                for spec in self._specs
            ],
        }


def _doc(action: ActionKind, operation: str) -> str:
    return f"docs/{action}/{operation.replace('_', '-')}.md"


OPERATION_REGISTRY = OperationRegistry(
    (
        OperationSpec(
            "open_session",
            "run",
            OpenSessionRequest,
            OpenSessionPayload,
            "dispatch_open_session",
            True,
            _doc("run", "open_session"),
        ),
        OperationSpec(
            "capture_flow",
            "run",
            CaptureFlowRequest,
            CaptureFlowPayload,
            "dispatch_capture_flow",
            True,
            _doc("run", "capture_flow"),
        ),
        OperationSpec(
            "replay_request",
            "run",
            ReplayRequestRequest,
            ReplayRequestPayload,
            "dispatch_replay_request",
            True,
            _doc("run", "replay_request"),
        ),
        OperationSpec(
            "save_script_source",
            "run",
            SaveScriptSourceRequest,
            SaveScriptSourcePayload,
            "dispatch_save_script_source",
            True,
            _doc("run", "save_script_source"),
        ),
        OperationSpec(
            "cancel_experiment",
            "run",
            CancelExperimentRequest,
            CancelExperimentPayload,
            "dispatch_cancel_experiment",
            True,
            _doc("run", "cancel_experiment"),
        ),
        OperationSpec(
            "close_session",
            "run",
            CloseSessionRequest,
            CloseSessionPayload,
            "dispatch_close_session",
            True,
            _doc("run", "close_session"),
        ),
        OperationSpec(
            "get_session",
            "inspect",
            GetSessionRequest,
            GetSessionPayload,
            "_inspect_get_session",
            False,
            _doc("inspect", "get_session"),
        ),
        OperationSpec(
            "list_experiments",
            "inspect",
            ListExperimentsRequest,
            ListExperimentsPayload,
            "_inspect_list_experiments",
            False,
            _doc("inspect", "list_experiments"),
        ),
        OperationSpec(
            "get_experiment",
            "inspect",
            GetExperimentRequest,
            GetExperimentPayload,
            "_inspect_get_experiment",
            False,
            _doc("inspect", "get_experiment"),
        ),
        OperationSpec(
            "get_stream_status",
            "inspect",
            GetStreamStatusRequest,
            GetStreamStatusPayload,
            "_inspect_get_stream_status",
            False,
            _doc("inspect", "get_stream_status"),
        ),
        OperationSpec(
            "list_evidence",
            "inspect",
            ListEvidenceRequest,
            ListEvidencePayload,
            "_inspect_list_evidence",
            False,
            _doc("inspect", "list_evidence"),
        ),
        OperationSpec(
            "get_network_evidence",
            "inspect",
            GetNetworkEvidenceRequest,
            GetNetworkEvidencePayload,
            "_inspect_get_network_evidence",
            False,
            _doc("inspect", "get_network_evidence"),
        ),
        OperationSpec(
            "get_request_shape",
            "inspect",
            GetRequestShapeRequest,
            GetRequestShapePayload,
            "_inspect_get_request_shape",
            False,
            _doc("inspect", "get_request_shape"),
        ),
        OperationSpec(
            "get_request_initiator",
            "inspect",
            GetRequestInitiatorRequest,
            GetRequestInitiatorPayload,
            "_inspect_get_request_initiator",
            False,
            _doc("inspect", "get_request_initiator"),
        ),
        OperationSpec(
            "search_scripts",
            "inspect",
            SearchScriptsRequest,
            SearchScriptsPayload,
            "_inspect_search_scripts",
            False,
            _doc("inspect", "search_scripts"),
        ),
        OperationSpec(
            "get_script_source",
            "inspect",
            GetScriptSourceRequest,
            GetScriptSourcePayload,
            "_inspect_get_script_source",
            False,
            _doc("inspect", "get_script_source"),
        ),
        OperationSpec(
            "list_console_errors",
            "inspect",
            ListConsoleErrorsRequest,
            ListConsoleErrorsPayload,
            "_inspect_list_console_errors",
            False,
            _doc("inspect", "list_console_errors"),
        ),
    )
)
