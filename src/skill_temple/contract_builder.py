"""Generate Browser operation structural contracts and version-bound examples."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .browser.contracts import ACTION_TRANSPORT_VERSION, expected_binding
from .browser.registry import OPERATION_REGISTRY, OperationSpec
from .content_hash import file_content_hash

_GENERATED_START = "<!-- BEGIN GENERATED CONTRACT -->"
_GENERATED_END = "<!-- END GENERATED CONTRACT -->"
_INDEX_START = "<!-- BEGIN GENERATED OPERATION TABLE -->"
_INDEX_END = "<!-- END GENERATED OPERATION TABLE -->"
_DECODED_RE = re.compile(
    r"## Decoded payload schema.*?```json\s*(\{.*?\})\s*```",
    re.DOTALL,
)
_ENVELOPE_SECTION_RE = re.compile(
    r"## Complete Action envelope\s+.*?(?=\n## |\Z)",
    re.DOTALL,
)
_GENERATED_RE = re.compile(
    re.escape(_GENERATED_START) + r".*?" + re.escape(_GENERATED_END),
    re.DOTALL,
)
_INDEX_RE = re.compile(
    re.escape(_INDEX_START) + r".*?" + re.escape(_INDEX_END),
    re.DOTALL,
)
_HASH_NOTE_RE = re.compile(r"^Contract hash:.*$", re.MULTILINE)


def _json(value: Any, *, compact: bool = False) -> str:
    if compact:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _decoded_example(text: str, spec: OperationSpec) -> dict[str, Any]:
    match = _DECODED_RE.search(text)
    if match is None:
        raise ValueError(f"Missing decoded payload example in {spec.contract_doc_path}")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid decoded payload example in {spec.contract_doc_path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ValueError(f"Decoded payload example must be an object: {spec.contract_doc_path}")
    try:
        spec.payload_model.model_validate(value)
    except ValidationError as exc:
        raise ValueError(
            f"Decoded payload example fails {spec.payload_model.__name__}: {exc}"
        ) from exc
    return value


def _envelope(
    spec: OperationSpec,
    payload: dict[str, Any],
    *,
    skill_content_hash: str,
) -> dict[str, str]:
    binding = expected_binding(spec, skill_content_hash=skill_content_hash)
    return {
        "contract_version": ACTION_TRANSPORT_VERSION,
        "operation": spec.name,
        "payload_json": _json(payload, compact=True),
        "skill_id": binding["skill_id"],
        "skill_content_hash": binding["skill_content_hash"],
        "operation_contract_hash": binding["operation_contract_hash"],
    }


def _generated_block(spec: OperationSpec) -> str:
    return "\n".join(
        [
            _GENERATED_START,
            "## Contract binding",
            "",
            "> Generated from the public operation contract. Do not edit this block.",
            "",
            f"- Action: `{spec.action}`",
            f"- Consequential: `{'true' if spec.consequential else 'false'}`",
            f"- Operation contract hash: `{spec.contract_hash}`",
            _GENERATED_END,
        ]
    )


def render_operation_doc(
    text: str,
    spec: OperationSpec,
    *,
    skill_content_hash: str,
) -> str:
    payload = _decoded_example(text, spec)
    envelope_section = "\n".join(
        [
            "## Complete Action envelope",
            "",
            "> Generated binding values are build-specific. Copy all six fields exactly.",
            "",
            "```json",
            _json(
                _envelope(
                    spec,
                    payload,
                    skill_content_hash=skill_content_hash,
                )
            ),
            "```",
        ]
    )
    if _ENVELOPE_SECTION_RE.search(text) is None:
        raise ValueError(f"Missing Complete Action envelope section: {spec.contract_doc_path}")
    rendered = _ENVELOPE_SECTION_RE.sub(envelope_section, text, count=1)
    rendered = _HASH_NOTE_RE.sub(
        f"Contract hash: `{spec.contract_hash}`. Send it in `operation_contract_hash`.",
        rendered,
    )
    block = _generated_block(spec)
    if _GENERATED_RE.search(rendered):
        rendered = _GENERATED_RE.sub(block, rendered, count=1)
    else:
        rendered = rendered.rstrip() + "\n\n" + block + "\n"
    return rendered.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def _operation_table() -> str:
    rows = [
        "| Operation | Action | Consequential | Contract path | Contract hash |",
        "|---|---|---:|---|---|",
    ]
    for spec in OPERATION_REGISTRY.specs():
        rows.append(
            f"| `{spec.name}` | `{spec.action}` | "
            f"`{'true' if spec.consequential else 'false'}` | "
            f"`{spec.contract_doc_path}` | `{spec.contract_hash}` |"
        )
    return "\n".join(
        [
            _INDEX_START,
            "## Generated registry table",
            "",
            "> Generated from `OperationRegistry`. Do not edit this table.",
            "",
            *rows,
            _INDEX_END,
        ]
    )


def build_contracts(protocol_root: str | Path) -> list[Path]:
    root = Path(protocol_root).expanduser().resolve()
    skill_path = root / "SKILL.md"
    if not skill_path.is_file():
        raise FileNotFoundError(f"Protocol Skill entrypoint not found: {skill_path}")
    skill_content_hash = file_content_hash(skill_path)
    written: list[Path] = []
    generated_root = root / "docs" / "generated"
    generated_root.mkdir(parents=True, exist_ok=True)
    for obsolete_dir in (generated_root / "run", generated_root / "inspect"):
        if obsolete_dir.exists():
            shutil.rmtree(obsolete_dir)

    catalog = OPERATION_REGISTRY.generated_catalog()
    catalog["protocol_skill_content_hash"] = skill_content_hash
    catalog_path = generated_root / "operation-contracts.json"
    catalog_path.write_text(_json(catalog) + "\n", encoding="utf-8", newline="\n")
    written.append(catalog_path)

    for spec in OPERATION_REGISTRY.specs():
        doc_path = root / spec.contract_doc_path
        if not doc_path.is_file():
            raise FileNotFoundError(f"Operation contract document not found: {doc_path}")
        rendered = render_operation_doc(
            doc_path.read_text(encoding="utf-8"),
            spec,
            skill_content_hash=skill_content_hash,
        )
        doc_path.write_text(rendered, encoding="utf-8", newline="\n")
        written.append(doc_path)

    index_path = root / "docs" / "operation-index.md"
    index_text = index_path.read_text(encoding="utf-8")
    table = _operation_table()
    if _INDEX_RE.search(index_text):
        index_text = _INDEX_RE.sub(table, index_text, count=1)
    else:
        index_text = index_text.rstrip() + "\n\n" + table + "\n"
    index_path.write_text(
        index_text.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n",
        encoding="utf-8",
        newline="\n",
    )
    written.append(index_path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Browser operation contracts from the registry and Pydantic."
    )
    parser.add_argument(
        "--protocol-root",
        type=Path,
        default=Path("src/skill_temple/example_skills/browser-action-protocol"),
    )
    args = parser.parse_args()
    for path in build_contracts(args.protocol_root):
        print(path)


if __name__ == "__main__":  # pragma: no cover
    main()
