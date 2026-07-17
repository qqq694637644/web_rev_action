"""Deterministic eval runner for the static catalog and exact Skill loading."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .runtime import load_runtime

PASS = "pass"
FAIL = "fail"


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            case = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSONL at {path}:{line_number}: {exc}") from exc
        case.setdefault("id", f"case-{line_number}")
        cases.append(case)
    return cases


def evaluate_case(case: dict[str, Any], skills_dir: Path | None = None) -> dict[str, Any]:
    runtime = load_runtime(skills_dir)
    expected_skill = str(case["expected_skill"])
    catalog_ids = [str(item["skill_id"]) for item in runtime.list_skills()["skills"]]
    catalog_ok = expected_skill in catalog_ids

    loaded = runtime.load_skills([expected_skill])
    packet = loaded["skills"][0]
    selected_ok = packet["skill_id"] == expected_skill
    referenced_paths = set(packet.get("referenced_paths", []))
    expected_paths = [str(path) for path in case.get("expected_paths", [])]
    missing_paths = [path for path in expected_paths if path not in referenced_paths]

    expected_symbols = [str(symbol) for symbol in case.get("expected_symbols", [])]
    symbol_text = packet["content"]
    unreadable_paths: list[str] = []
    for path in expected_paths:
        try:
            symbol_text += "\n" + runtime.read(expected_skill, path, max_lines=5000)["content"]
        except Exception:
            unreadable_paths.append(path)
    missing_symbols = [symbol for symbol in expected_symbols if symbol not in symbol_text]

    ok = (
        catalog_ok
        and selected_ok
        and not missing_paths
        and not unreadable_paths
        and not missing_symbols
    )
    return {
        "id": case["id"],
        "status": PASS if ok else FAIL,
        "query": str(case.get("query", "")),
        "expected_skill": expected_skill,
        "catalog_ok": catalog_ok,
        "selected_ok": selected_ok,
        "expected_paths": expected_paths,
        "referenced_paths": sorted(referenced_paths),
        "missing_paths": missing_paths,
        "unreadable_paths": unreadable_paths,
        "expected_symbols": expected_symbols,
        "missing_symbols": missing_symbols,
    }


def evaluate_file(path: Path, skills_dir: Path | None = None) -> dict[str, Any]:
    cases = load_cases(path)
    results = [evaluate_case(case, skills_dir=skills_dir) for case in cases]
    failed = [result for result in results if result["status"] != PASS]
    return {
        "case_count": len(results),
        "passed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate static Skill catalog and loading.")
    parser.add_argument("cases", type=Path, help="Path to a JSONL eval file.")
    parser.add_argument("--skills-dir", type=Path, default=None)
    args = parser.parse_args()

    report = evaluate_file(args.cases, skills_dir=args.skills_dir)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
