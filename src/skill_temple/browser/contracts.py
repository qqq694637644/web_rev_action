"""Browser protocol Skill and operation contract version binding."""

from __future__ import annotations

from importlib import resources

from ..content_hash import text_content_hash
from .registry import OPERATION_REGISTRY, OperationSpec

PROTOCOL_SKILL_ID = "browser-action-protocol"
ACTION_TRANSPORT_VERSION = "2.0"


def protocol_skill_content_hash() -> str:
    """Hash the exact packaged browser protocol Skill entrypoint."""

    content = (
        resources.files("skill_temple")
        .joinpath("example_skills", PROTOCOL_SKILL_ID, "SKILL.md")
        .read_text(encoding="utf-8")
    )
    return text_content_hash(content)


def expected_binding(
    spec: OperationSpec,
    *,
    skill_content_hash: str | None = None,
) -> dict[str, str]:
    return {
        "action_transport_version": ACTION_TRANSPORT_VERSION,
        "operation": spec.name,
        "skill_id": PROTOCOL_SKILL_ID,
        "skill_content_hash": skill_content_hash or protocol_skill_content_hash(),
        "operation_contract_hash": spec.contract_hash,
    }


def expected_binding_for_operation(operation: str) -> dict[str, str]:
    return expected_binding(OPERATION_REGISTRY.require(operation))
