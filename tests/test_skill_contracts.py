from __future__ import annotations

import json
import re
from pathlib import Path

from skill_temple.browser.transport import (
    INSPECT_OPERATIONS,
    RUN_OPERATIONS,
    decode_inspect_envelope,
    decode_run_envelope,
)
from skill_temple.browser_models import (
    InspectBrowserEvidenceEnvelope,
    RunBrowserExperimentEnvelope,
)
from skill_temple.runtime import load_runtime

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = (
    ROOT
    / "src"
    / "skill_temple"
    / "example_skills"
    / "browser-action-protocol"
)
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _operation_doc_path(operation: str, action: str) -> Path:
    return PROTOCOL_ROOT / "docs" / action / f"{operation.replace('_', '-')}.md"


def _full_envelopes(text: str) -> list[dict[str, object]]:
    envelopes: list[dict[str, object]] = []
    for raw in _JSON_BLOCK_RE.findall(text):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and set(value) == {
            "contract_version",
            "operation",
            "payload_json",
            "skill_id",
            "skill_content_hash",
            "operation_contract_hash",
        }:
            envelopes.append(value)
    return envelopes


def test_protocol_skill_references_all_required_documents() -> None:
    runtime = load_runtime(PROTOCOL_ROOT.parent)
    packet = runtime.load_skills(["browser-action-protocol"])["skills"][0]
    referenced = set(packet["referenced_paths"])
    assert {
        "docs/transport-envelope.md",
        "docs/json-encoding.md",
        "docs/error-recovery.md",
        "docs/operation-index.md",
    }.issubset(referenced)
    for path in referenced:
        result = runtime.read("browser-action-protocol", path)
        assert result["path"] == path


def test_every_browser_operation_has_one_valid_complete_envelope_example() -> None:
    for action, operations in [
        ("run", RUN_OPERATIONS),
        ("inspect", INSPECT_OPERATIONS),
    ]:
        for operation in sorted(operations):
            path = _operation_doc_path(operation, action)
            assert path.is_file(), path
            text = path.read_text(encoding="utf-8")
            envelopes = _full_envelopes(text)
            assert envelopes, f"missing complete envelope example: {path}"
            envelope = envelopes[0]
            assert envelope["contract_version"] == "2.0"
            assert envelope["operation"] == operation
            assert isinstance(envelope["payload_json"], str)
            decoded = json.loads(envelope["payload_json"])
            assert isinstance(decoded, dict)
            assert "payload" not in envelope
            if action == "run":
                request = decode_run_envelope(
                    RunBrowserExperimentEnvelope.model_validate(envelope)
                )
            else:
                request = decode_inspect_envelope(
                    InspectBrowserEvidenceEnvelope.model_validate(envelope)
                )
            assert request.operation == operation


def test_protocol_docs_do_not_expose_removed_alias() -> None:
    content = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(PROTOCOL_ROOT.rglob("*.md"))
    )
    assert "capture" + "_baseline" not in content


def test_non_history_public_surface_has_no_removed_skill_actions_or_alias() -> None:
    paths = [
        ROOT / "src",
        ROOT / "tests",
        ROOT / "tools",
        ROOT / "validation",
        ROOT / "README.md",
        ROOT / "INSTALL.md",
        ROOT / "GPT_ACTION_PROMPT.md",
    ]
    content_parts: list[str] = []
    for path in paths:
        if path.is_dir():
            files = sorted(item for item in path.rglob("*") if item.suffix in {".py", ".md"})
        else:
            files = [path]
        for file_path in files:
            content_parts.append(file_path.read_text(encoding="utf-8"))
    content = "\n".join(content_parts)
    assert "retrieve" + "SkillContext" not in content
    assert "search" + "SkillDocs" not in content
    assert "capture" + "_baseline" not in content
