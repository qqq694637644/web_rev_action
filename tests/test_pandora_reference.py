from __future__ import annotations

import json
import re
from pathlib import Path

from skill_temple.browser.registry import OPERATION_REGISTRY

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "PANDORA_REPRODUCTION.md"
FORBIDDEN_TOKENS = {
    "control_experiment_id",
    "pair_protocol_hash",
    "fresh_equivalent",
    "same_value",
    "runBrowserExperiment.trace_request",
}


def test_pandora_reference_contains_no_obsolete_contract_tokens() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    for token in FORBIDDEN_TOKENS:
        assert token not in text


def test_pandora_reference_json_examples_validate_against_current_payload_models() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    examples = re.findall(
        r"```json operation=([a-z_]+)\n(.*?)\n```",
        text,
        flags=re.DOTALL,
    )
    assert examples, "expected operation-tagged JSON examples"

    operations: set[str] = set()
    for operation, raw in examples:
        operations.add(operation)
        payload = json.loads(raw)
        spec = OPERATION_REGISTRY.require(operation)
        spec.payload_model.model_validate(payload)

    assert {"open_session", "capture_flow", "replay_request"} <= operations
