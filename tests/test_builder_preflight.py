from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from skill_temple.builder_preflight import run_preflight

ROOT = Path(__file__).resolve().parents[1]


def test_builder_preflight_passes_for_committed_repository_inputs() -> None:
    report = run_preflight(repository_root=ROOT)
    assert report["ok"] is True, report
    assert report["checks"]["generated_instructions"]["ok"] is True
    assert report["checks"]["public_operation_ids"]["ok"] is True
    assert report["checks"]["browser_envelopes"]["ok"] is True
    assert report["checks"]["contract_binding"]["ok"] is True
    assert report["checks"]["release_checklist"]["ok"] is True
    assert report["manual_gate"]["required"] is True


def test_builder_preflight_detects_invalid_instructions_template() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "src/skill_temple").mkdir(parents=True)
        source_skills = ROOT / "src/skill_temple/example_skills"
        target_skills = root / "src/skill_temple/example_skills"
        shutil.copytree(source_skills, target_skills)
        (root / "GPT_ACTION_PROMPT.md").write_text(
            "instructions without a catalog placeholder\n", encoding="utf-8"
        )
        report = run_preflight(repository_root=root)
    assert report["ok"] is False
    assert report["checks"]["generated_instructions"]["ok"] is False


def test_builder_preflight_detects_committed_instructions_drift() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        source_skills = ROOT / "src/skill_temple/example_skills"
        target_skills = root / "src/skill_temple/example_skills"
        shutil.copytree(source_skills, target_skills)
        shutil.copy(ROOT / "GPT_ACTION_PROMPT.md", root / "GPT_ACTION_PROMPT.md")
        shutil.copy(ROOT / "BUILDER_SMOKE_CHECKLIST.md", root / "BUILDER_SMOKE_CHECKLIST.md")
        (root / "GPT_INSTRUCTIONS.sha256").write_text("0" * 64 + "\n", encoding="utf-8")

        report = run_preflight(repository_root=root)

    generated = report["checks"]["generated_instructions"]
    assert report["ok"] is False
    assert generated["matches_expected"] is False
    assert generated["generated_sha256"] != generated["expected_sha256"]
