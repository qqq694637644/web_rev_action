"""Compile the static Skill catalog into GPT Instructions."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from .runtime import SkillRuntime, load_runtime

CATALOG_PLACEHOLDER = "{{SKILL_CATALOG}}"


def _single_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def render_catalog(runtime: SkillRuntime) -> str:
    """Render deterministic Skill metadata without disclosing Skill bodies."""

    skills = runtime.list_skills()["skills"]
    return "\n".join(
        f"- {_single_line(str(skill['name']))}: "
        f"{_single_line(str(skill['description']))} "
        f"(skill_id: {_single_line(str(skill['skill_id']))})"
        for skill in skills
    )


def build_instructions(
    *,
    runtime: SkillRuntime,
    template_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Replace the required catalog placeholder and write normalized LF output."""

    template = Path(template_path)
    output = Path(output_path)
    text = template.read_text(encoding="utf-8")
    if CATALOG_PLACEHOLDER not in text:
        raise ValueError(f"Instructions template is missing {CATALOG_PLACEHOLDER}")
    rendered = text.replace(CATALOG_PLACEHOLDER, render_catalog(runtime))
    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compile the static Skill catalog into GPT Instructions."
    )
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--template", type=Path, default=Path("GPT_ACTION_PROMPT.md"))
    parser.add_argument(
        "--output", type=Path, default=Path("dist/GPT_INSTRUCTIONS.md")
    )
    args = parser.parse_args()
    path = build_instructions(
        runtime=load_runtime(args.skills_dir),
        template_path=args.template,
        output_path=args.output,
    )
    print(path)


if __name__ == "__main__":  # pragma: no cover
    main()
