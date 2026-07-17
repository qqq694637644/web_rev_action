from __future__ import annotations

import tempfile
from pathlib import Path

from skill_temple.dead_code_audit import audit_repository

ROOT = Path(__file__).resolve().parents[1]


def test_repository_passes_dead_code_audit() -> None:
    report = audit_repository(ROOT)
    assert report["violation_count"] == 0, report


def test_audit_detects_removed_public_browser_payload_transport() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "README.md").write_text(
            '{"operation":"get_session","payload":{"session_id":"one"}}\n',
            encoding="utf-8",
        )
        report = audit_repository(root)
    assert report["violation_count"] >= 1
    assert any(
        item["reason"] == "removed public Browser nested payload transport"
        for item in report["violations"]
    )


def test_audit_detects_removed_skill_action_identifier() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "README.md").write_text(
            "old operation: " + "retrieve" + "SkillContext\n",
            encoding="utf-8",
        )
        report = audit_repository(root)
    assert report["violation_count"] >= 1
    assert any(
        item["reason"] == "removed public Skill retrieval operation"
        for item in report["violations"]
    )


def test_audit_scans_current_root_documents() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "PANDORA_REPRODUCTION.md").write_text(
            "legacy alias: " + "capture" + "_baseline\n",
            encoding="utf-8",
        )
        report = audit_repository(root)

    assert any(
        item["path"] == "PANDORA_REPRODUCTION.md"
        and item["reason"] == "removed Browser alias"
        for item in report["violations"]
    )
