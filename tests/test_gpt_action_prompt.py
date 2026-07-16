from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "GPT_ACTION_PROMPT.md"


def test_gpt56_action_prompt_stays_lean() -> None:
    content = PROMPT_PATH.read_text(encoding="utf-8")

    assert len(content.encode("utf-8")) <= 7_000
    assert len(content.splitlines()) <= 100


def test_gpt56_action_prompt_routes_details_to_skills() -> None:
    content = PROMPT_PATH.read_text(encoding="utf-8")

    assert "{{SKILL_CATALOG}}" in content
    assert "loadSkills" in content
    assert "readSkillContent" in content
    assert "browser-action-protocol" in content
    assert "retrieve" + "SkillContext" not in content
    assert "search" + "SkillDocs" not in content

    detailed_workflow_terms = {
        "requested_replay_protocol",
        "final_wire_observability",
        "terminal_condition_matched",
        "query_serialization=preserve_raw",
        "BrowserActionService",
        "protocol_evidence.py",
        "tests/runtime/replay_runtime.test.js",
    }
    assert detailed_workflow_terms.isdisjoint(content)
