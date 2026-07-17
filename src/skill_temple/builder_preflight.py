"""Local preflight for the externally executed GPT Builder release smoke."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from .app import create_app
from .browser.registry import OPERATION_REGISTRY
from .prompt_builder import build_instructions
from .runtime import load_runtime

EXPECTED_OPERATION_IDS = {
    "loadSkills",
    "readSkillContent",
    "inspectBrowserEvidence",
    "runBrowserExperiment",
    "workspaceInspect",
    "workspaceSearch",
    "workspaceReadFiles",
    "workspaceWriteFile",
    "workspaceApplyPatch",
    "workspaceExecPwsh",
}
EXPECTED_BROWSER_FIELDS = {
    "contract_version",
    "operation",
    "payload_json",
    "skill_id",
    "skill_content_hash",
    "operation_contract_hash",
}
_FORBIDDEN_COMPOSITION = {"oneOf", "anyOf", "allOf", "discriminator"}


def _resolve_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    reference = schema.get("$ref")
    if not isinstance(reference, str):
        return schema
    return root["components"]["schemas"][reference.rsplit("/", 1)[-1]]


def _composition_keys(value: Any, path: str = "$") -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}/{key}"
            if key in _FORBIDDEN_COMPOSITION:
                matches.append(child_path)
            matches.extend(_composition_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            matches.extend(_composition_keys(child, f"{path}/{index}"))
    return matches


def run_preflight(
    *,
    repository_root: str | Path,
    skills_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(repository_root).expanduser().resolve()
    resolved_skills = (
        Path(skills_dir).expanduser().resolve()
        if skills_dir
        else root / "src/skill_temple/example_skills"
    )
    runtime = load_runtime(resolved_skills)
    catalog = runtime.list_skills()["skills"]
    catalog_ids = [str(item["skill_id"]) for item in catalog]

    checks: dict[str, dict[str, Any]] = {}

    template_path = root / "GPT_ACTION_PROMPT.md"
    generated_ok = False
    placeholder_absent = False
    generated_sha256: str | None = None
    expected_sha256: str | None = None
    matches_expected = False
    generation_error: str | None = None
    hash_path = root / "GPT_INSTRUCTIONS.sha256"
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            generated = Path(temp_dir) / "GPT_INSTRUCTIONS.md"
            build_instructions(
                runtime=runtime,
                template_path=template_path,
                output_path=generated,
            )
            generated_bytes = generated.read_bytes()
            generated_ok = bool(generated_bytes)
            placeholder_absent = b"{{SKILL_CATALOG}}" not in generated_bytes
            generated_sha256 = hashlib.sha256(generated_bytes).hexdigest()
            if hash_path.is_file():
                expected_sha256 = hash_path.read_text(encoding="utf-8").strip()
                matches_expected = expected_sha256 == generated_sha256
    except (OSError, ValueError) as exc:
        generation_error = str(exc)
    checks["generated_instructions"] = {
        "ok": generated_ok and placeholder_absent and matches_expected,
        "catalog_skill_count": len(catalog_ids),
        "catalog_skill_ids": catalog_ids,
        "placeholder_absent": placeholder_absent,
        "generated_sha256": generated_sha256,
        "expected_sha256": expected_sha256,
        "matches_expected": matches_expected,
        "hash_path": "GPT_INSTRUCTIONS.sha256",
        "error": generation_error,
    }

    schema = create_app(skills_dir=resolved_skills).openapi()
    operation_ids = {
        operation["operationId"]
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    checks["public_operation_ids"] = {
        "ok": operation_ids == EXPECTED_OPERATION_IDS,
        "actual": sorted(operation_ids),
        "expected": sorted(EXPECTED_OPERATION_IDS),
    }

    browser_checks: dict[str, Any] = {}
    browser_ok = True
    for route in ["/v1/browser/inspect", "/v1/browser/run"]:
        operation = schema["paths"][route]["post"]
        request_schema = _resolve_schema(
            operation["requestBody"]["content"]["application/json"]["schema"],
            schema,
        )
        fields = set(request_schema.get("properties", {}))
        required = set(request_schema.get("required", []))
        composition = _composition_keys(request_schema)
        route_ok = (
            fields == EXPECTED_BROWSER_FIELDS
            and required == EXPECTED_BROWSER_FIELDS
            and not composition
        )
        browser_ok = browser_ok and route_ok
        browser_checks[route] = {
            "ok": route_ok,
            "fields": sorted(fields),
            "required": sorted(required),
            "forbidden_composition_paths": composition,
            "consequential": operation.get("x-openai-isConsequential"),
        }
    checks["browser_envelopes"] = {"ok": browser_ok, "routes": browser_checks}

    protocol = next(
        (item for item in catalog if item["skill_id"] == "browser-action-protocol"),
        None,
    )
    generated_catalog_path = (
        resolved_skills
        / "browser-action-protocol"
        / "docs/generated/operation-contracts.json"
    )
    generated_catalog = (
        json.loads(generated_catalog_path.read_text(encoding="utf-8"))
        if generated_catalog_path.is_file()
        else {}
    )
    generated_operations = generated_catalog.get("operations", [])
    binding_ok = bool(
        protocol
        and generated_catalog.get("protocol_skill_content_hash")
        == protocol.get("content_hash")
        and len(generated_operations) == len(OPERATION_REGISTRY.operations())
        and {
            str(item.get("operation"))
            for item in generated_operations
            if isinstance(item, dict)
        }
        == set(OPERATION_REGISTRY.operations())
    )
    checks["contract_binding"] = {
        "ok": binding_ok,
        "protocol_skill_content_hash": (
            protocol.get("content_hash") if protocol else None
        ),
        "generated_protocol_skill_content_hash": generated_catalog.get(
            "protocol_skill_content_hash"
        ),
        "operation_count": len(generated_operations),
    }

    checklist_path = root / "BUILDER_SMOKE_CHECKLIST.md"
    checklist_ok = checklist_path.is_file() and checklist_path.stat().st_size > 0
    checks["release_checklist"] = {
        "ok": checklist_ok,
        "path": "BUILDER_SMOKE_CHECKLIST.md",
    }

    ok = all(bool(check.get("ok")) for check in checks.values())
    return {
        "format": "gpt-builder-preflight-v1",
        "ok": ok,
        "checks": checks,
        "manual_gate": {
            "required": True,
            "checklist": "BUILDER_SMOKE_CHECKLIST.md",
            "reason": (
                "Authenticated GPT Builder import, tool rendering, model Skill selection, "
                "and live browser execution cannot be verified by repository CI."
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate repository inputs before the authenticated GPT Builder smoke."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    report = run_preflight(repository_root=args.root, skills_dir=args.skills_dir)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
