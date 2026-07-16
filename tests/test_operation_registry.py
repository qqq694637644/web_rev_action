from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from skill_temple.browser import dispatcher
from skill_temple.browser.contracts import expected_binding
from skill_temple.browser.registry import OPERATION_REGISTRY
from skill_temple.browser_service import BrowserActionService
from skill_temple.contract_builder import build_contracts

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = (
    ROOT
    / "src"
    / "skill_temple"
    / "example_skills"
    / "browser-action-protocol"
)

EXPECTED_RUN = {
    "open_session",
    "capture_flow",
    "replay_request",
    "save_script_source",
    "cancel_experiment",
    "close_session",
}
EXPECTED_INSPECT = {
    "get_session",
    "list_experiments",
    "get_experiment",
    "get_stream_status",
    "list_evidence",
    "get_network_evidence",
    "get_request_shape",
    "get_request_initiator",
    "search_scripts",
    "get_script_source",
    "list_console_errors",
}


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_registry_is_the_complete_operation_source() -> None:
    assert set(OPERATION_REGISTRY.operations("run")) == EXPECTED_RUN
    assert set(OPERATION_REGISTRY.operations("inspect")) == EXPECTED_INSPECT
    assert len(OPERATION_REGISTRY.operations()) == 17

    for spec in OPERATION_REGISTRY.specs():
        assert spec.consequential is (spec.action == "run")
        assert spec.request_model.model_fields["operation"].annotation is not None
        assert spec.payload_model.model_json_schema(mode="validation")["type"] == "object"
        assert spec.contract_hash.startswith("sha256:")
        assert len(spec.contract_hash) == 71
        assert (PROTOCOL_ROOT / spec.contract_doc_path).is_file()
        if spec.action == "run":
            assert callable(getattr(dispatcher, spec.handler_name, None))
        else:
            assert callable(getattr(BrowserActionService, spec.handler_name, None))


def test_generated_registry_catalog_matches_committed_artifact() -> None:
    committed = json.loads(
        (PROTOCOL_ROOT / "docs/generated/operation-contracts.json").read_text(
            encoding="utf-8"
        )
    )
    expected = OPERATION_REGISTRY.generated_catalog()
    expected["protocol_skill_content_hash"] = expected_binding(
        OPERATION_REGISTRY.specs()[0]
    )["skill_content_hash"]
    assert committed == expected


def test_contract_generation_is_deterministic_and_committed_without_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        copied = Path(temp_dir) / "browser-action-protocol"
        shutil.copytree(PROTOCOL_ROOT, copied)
        build_contracts(copied)
        first = _tree_hashes(copied)
        build_contracts(copied)
        second = _tree_hashes(copied)
        assert first == second
        assert first == _tree_hashes(PROTOCOL_ROOT)


def test_contract_generation_binds_the_selected_protocol_root_skill_hash() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        copied = Path(temp_dir) / "browser-action-protocol"
        shutil.copytree(PROTOCOL_ROOT, copied)
        skill_path = copied / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8") + "\nCustom protocol build marker.\n",
            encoding="utf-8",
        )
        expected_skill_hash = f"sha256:{hashlib.sha256(skill_path.read_bytes()).hexdigest()}"
        build_contracts(copied)

        catalog = json.loads(
            (copied / "docs/generated/operation-contracts.json").read_text(
                encoding="utf-8"
            )
        )
        assert catalog["protocol_skill_content_hash"] == expected_skill_hash

        open_session = (copied / "docs/run/open-session.md").read_text(encoding="utf-8")
        assert f'"skill_content_hash": "{expected_skill_hash}"' in open_session


def test_operation_hash_changes_when_structural_metadata_changes() -> None:
    spec = OPERATION_REGISTRY.require("get_session")
    original = spec.contract_hash
    generated = spec.generated_contract()
    generated["handler_name"] = "different_handler"
    canonical = json.dumps(
        generated,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    changed = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    assert changed != original
