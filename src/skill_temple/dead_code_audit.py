"""Fail when removed Skill/Browser compatibility surfaces reappear."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from .app import create_app
from .browser.registry import OPERATION_REGISTRY
from .runtime import SkillRuntime

TEXT_SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".toml"}
SCAN_ROOTS = [
    "src",
    "tests",
    "tools",
    "validation",
    ".github",
    "README.md",
    "INSTALL.md",
    "GPT_ACTION_PROMPT.md",
]
SKIP_FILES = {
    "src/skill_temple/dead_code_audit.py",
    "tools/dead_code_audit.py",
}
FORBIDDEN_TEXT = {
    "retrieve" + "SkillContext": "removed public Skill retrieval operation",
    "search" + "SkillDocs": "removed public Skill search operation",
    "capture" + "_baseline": "removed Browser alias",
    "normalize_browser_action_" + "openapi": "removed OpenAPI schema rewriting",
    "_request_object_" + "schema": "removed OpenAPI request schema rewriting",
    "sqlite_fts5_" + "symbol_index": "removed SQLite FTS runtime",
}
PUBLIC_DOC_PATHS = [
    "src/skill_temple/example_skills",
    "README.md",
    "INSTALL.md",
    "GPT_ACTION_PROMPT.md",
]
_NESTED_PAYLOAD_RE = re.compile(r"[\"']payload[\"']\s*:")


def _files(root: Path) -> list[Path]:
    selected: list[Path] = []
    for relative in SCAN_ROOTS:
        path = root / relative
        if path.is_file():
            selected.append(path)
        elif path.is_dir():
            selected.extend(
                child
                for child in path.rglob("*")
                if child.is_file()
                and child.suffix.lower() in TEXT_SUFFIXES
                and ".venv" not in child.parts
                and "__pycache__" not in child.parts
            )
    return sorted(set(selected))


def audit_repository(root: str | Path) -> dict[str, Any]:
    repository = Path(root).expanduser().resolve()
    violations: list[dict[str, Any]] = []
    files = _files(repository)
    for path in files:
        relative = path.relative_to(repository).as_posix()
        if relative in SKIP_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for token, reason in FORBIDDEN_TEXT.items():
            for line_number, line in enumerate(text.splitlines(), start=1):
                if token in line:
                    violations.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "token": token,
                            "reason": reason,
                        }
                    )

    for relative_root in PUBLIC_DOC_PATHS:
        public_path = repository / relative_root
        public_files = (
            [public_path]
            if public_path.is_file()
            else [
                item
                for item in public_path.rglob("*")
                if item.is_file() and item.suffix.lower() in {".md", ".json"}
            ]
        )
        for path in public_files:
            text = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if _NESTED_PAYLOAD_RE.search(line):
                    violations.append(
                        {
                            "path": path.relative_to(repository).as_posix(),
                            "line": line_number,
                            "token": '"payload":',
                            "reason": "removed public Browser nested payload transport",
                        }
                    )

    for path in files:
        if path.suffix.lower() != ".py":
            continue
        relative = path.relative_to(repository).as_posix()
        if relative in SKIP_FILES:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "/v1/browser/" not in line:
                continue
            for offset, candidate in enumerate(lines[index : index + 30], start=0):
                if _NESTED_PAYLOAD_RE.search(candidate):
                    violations.append(
                        {
                            "path": relative,
                            "line": index + offset + 1,
                            "token": '"payload":',
                            "reason": (
                                "Browser endpoint call uses the removed nested payload "
                                "transport"
                            ),
                        }
                    )

    runtime_methods = set(dir(SkillRuntime))
    for method in ["resolve", "retrieve", "search"]:
        if method in runtime_methods:
            violations.append(
                {
                    "path": "src/skill_temple/runtime.py",
                    "line": 0,
                    "token": method,
                    "reason": "removed dynamic Skill runtime method is present",
                }
            )

    schema = create_app().openapi()
    operation_ids = {
        operation["operationId"]
        for path_item in schema.get("paths", {}).values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    expected_browser = {"inspectBrowserEvidence", "runBrowserExperiment"}
    actual_browser = {
        item for item in operation_ids if item in expected_browser or "Browser" in item
    }
    if actual_browser != expected_browser:
        violations.append(
            {
                "path": "openapi.json",
                "line": 0,
                "token": sorted(actual_browser),
                "reason": "Browser Action public surface differs from two stable operations",
            }
        )
    for path in ["/v1/browser/inspect", "/v1/browser/run"]:
        request_schema = schema["paths"][path]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        if "$ref" in request_schema:
            component_name = request_schema["$ref"].rsplit("/", 1)[-1]
            request_schema = schema["components"]["schemas"][component_name]
        properties = set(request_schema.get("properties", {}))
        expected_properties = {
            "contract_version",
            "operation",
            "payload_json",
            "skill_id",
            "skill_content_hash",
            "operation_contract_hash",
        }
        if properties != expected_properties:
            violations.append(
                {
                    "path": "openapi.json",
                    "line": 0,
                    "token": sorted(properties),
                    "reason": (
                        f"{path} request properties differ from the version-bound "
                        "Browser envelope"
                    ),
                }
            )
    if set(OPERATION_REGISTRY.operations()) & {"capture" + "_baseline"}:
        violations.append(
            {
                "path": "src/skill_temple/browser/registry.py",
                "line": 0,
                "token": "capture" + "_baseline",
                "reason": "removed alias is registered",
            }
        )

    return {
        "format": "web-rev-action-dead-code-audit-v1",
        "scanned_file_count": len(files),
        "violation_count": len(violations),
        "violations": violations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit removed Skill and Browser compatibility surfaces."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    report = audit_repository(args.root)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["violation_count"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
