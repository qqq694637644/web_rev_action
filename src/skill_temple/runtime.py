"""Exact Codex-style Skill loading and safe progressive disclosure."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .content_hash import file_content_hash

_SKILL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKTICK_PATH_RE = re.compile(r"`((?:docs|references|scripts|assets)/[^`\r\n]*)`")
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

DEFAULT_MAX_SKILLS = 3
DOTENV_FILE_NAME = ".env"
MAX_SKILL_SCAN_DEPTH = 6
SKILL_NAME_MAX_CHARS = 64
SKILL_DESCRIPTION_MAX_CHARS = 1024


class SkillRuntimeError(RuntimeError):
    """Base error for Skill runtime failures."""


class SkillNotFoundError(SkillRuntimeError):
    """Raised when a requested exact skill id is unavailable."""


class SkillPathError(SkillRuntimeError):
    """Raised when a requested Skill path is invalid, unsafe, or unavailable."""


class SkillLineLimitError(SkillRuntimeError):
    """Raised when one line cannot fit inside the bounded Skill read contract."""

    def __init__(self, *, path: str, line_number: int, actual_chars: int, max_chars: int) -> None:
        super().__init__(
            f"Skill line exceeds read limit: path={path!r}, line={line_number}, "
            f"actual_chars={actual_chars}, max_chars={max_chars}"
        )
        self.path = path
        self.line_number = line_number
        self.actual_chars = actual_chars
        self.max_chars = max_chars


@dataclass(frozen=True)
class Skill:
    """A discovered SKILL.md entrypoint."""

    skill_id: str
    root: Path
    name: str
    description: str
    content_hash: str
    referenced_paths: tuple[str, ...] = ()

    @property
    def entrypoint(self) -> str:
        return "SKILL.md"

    @property
    def source_path(self) -> str:
        return f"{self.skill_id}/{self.entrypoint}"


def load_runtime(skills_dir: str | Path | None = None) -> SkillRuntime:
    """Create a runtime from an explicit path, environment, cwd, or packaged Skills."""

    return SkillRuntime(_resolve_skills_dir(skills_dir))


def _resolve_skills_dir(skills_dir: str | Path | None) -> Path:
    if skills_dir:
        return Path(skills_dir).expanduser().resolve()

    env_value = env_value_from_environment_or_dotenv("SKILL_TEMPLE_SKILLS_DIR")
    if env_value:
        return Path(env_value).expanduser().resolve()

    with resources.as_file(resources.files("skill_temple") / "example_skills") as path:
        return path.resolve()


def env_value_from_environment_or_dotenv(name: str) -> str | None:
    """Return an environment value, falling back to the current directory .env file."""

    value = os.environ.get(name)
    if value:
        return value
    return _read_dotenv_file(Path.cwd() / DOTENV_FILE_NAME).get(name)


def _read_dotenv_file(path: Path) -> dict[str, str]:
    if not path.exists() or not path.is_file():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_dotenv_line(line)
        if parsed is not None:
            key, value = parsed
            values[key] = value
    return values


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[7:].lstrip()
    if "=" not in stripped:
        return None

    key, raw_value = stripped.split("=", 1)
    key = key.strip()
    if not _ENV_KEY_RE.fullmatch(key):
        return None

    value = raw_value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return key, value[1:-1]
    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
    return key, value


def _safe_skill_id(skill_id: str) -> str:
    if not _SKILL_ID_RE.fullmatch(skill_id):
        raise SkillNotFoundError(f"Invalid skill_id: {skill_id!r}")
    return skill_id


def _unique_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _content_hash(path: Path) -> str:
    return file_content_hash(path)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillRuntimeError("SKILL.md is missing YAML frontmatter")

    try:
        closing = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise SkillRuntimeError("SKILL.md frontmatter is not closed with ---") from exc

    parsed = yaml.safe_load("\n".join(lines[1:closing])) or {}
    if not isinstance(parsed, dict):
        raise SkillRuntimeError("SKILL.md frontmatter must be a YAML mapping")
    return {str(key): value for key, value in parsed.items()}, "\n".join(lines[closing + 1 :])


class SkillRuntime:
    """Discover Skills, load exact entrypoints, and read exact relative paths."""

    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir).expanduser().resolve()
        if not self.skills_dir.exists():
            raise FileNotFoundError(f"Skills directory does not exist: {self.skills_dir}")
        if not self.skills_dir.is_dir():
            raise NotADirectoryError(f"Skills path is not a directory: {self.skills_dir}")
        self._skills = self._load_skills()

    def _load_skills(self) -> dict[str, Skill]:
        skills: dict[str, Skill] = {}
        for manifest_path in self._iter_skill_manifests():
            skill = self._load_skill(manifest_path)
            if skill.skill_id in skills:
                other = skills[skill.skill_id].root
                raise SkillRuntimeError(
                    f"Duplicate skill name {skill.skill_id!r}: {other} and {skill.root}"
                )
            skills[skill.skill_id] = skill
        return dict(sorted(skills.items()))

    def _iter_skill_manifests(self) -> list[Path]:
        manifests: list[Path] = []
        for current_root, dir_names, file_names in os.walk(self.skills_dir, topdown=True):
            current_path = Path(current_root)
            depth = len(current_path.relative_to(self.skills_dir).parts)
            dir_names[:] = sorted(
                name
                for name in dir_names
                if not name.startswith(".") and name != "__pycache__"
            )
            if depth >= MAX_SKILL_SCAN_DEPTH:
                dir_names[:] = []
            if "SKILL.md" in file_names:
                manifests.append(current_path / "SKILL.md")
        return sorted(manifests)

    def _load_skill(self, manifest_path: Path) -> Skill:
        text = manifest_path.read_text(encoding="utf-8", errors="strict")
        frontmatter, _body = _parse_frontmatter(text)
        name = str(frontmatter.get("name") or "").strip()
        description = str(frontmatter.get("description") or "").strip()
        if not name:
            raise SkillRuntimeError(f"SKILL.md is missing frontmatter name: {manifest_path}")
        if not description:
            raise SkillRuntimeError(
                f"SKILL.md is missing frontmatter description: {manifest_path}"
            )
        if len(name) > SKILL_NAME_MAX_CHARS:
            raise SkillRuntimeError(
                f"SKILL.md frontmatter name exceeds {SKILL_NAME_MAX_CHARS} characters: "
                f"{manifest_path}"
            )
        if len(description) > SKILL_DESCRIPTION_MAX_CHARS:
            raise SkillRuntimeError(
                "SKILL.md frontmatter description exceeds "
                f"{SKILL_DESCRIPTION_MAX_CHARS} characters: {manifest_path}"
            )
        skill_id = _safe_skill_id(name)
        skill = Skill(
            skill_id=skill_id,
            root=manifest_path.parent.resolve(),
            name=name,
            description=description,
            content_hash=_content_hash(manifest_path),
        )
        return Skill(
            skill_id=skill.skill_id,
            root=skill.root,
            name=skill.name,
            description=skill.description,
            content_hash=skill.content_hash,
            referenced_paths=tuple(
                self._referenced_paths(skill, text, source_path=skill.entrypoint)
            ),
        )

    def list_skills(self) -> dict[str, Any]:
        """Return metadata for hidden diagnostics and prompt compilation only."""

        return {
            "skills_dir": str(self.skills_dir),
            "skills": [self._public_skill_metadata(skill) for skill in self._skills.values()],
        }

    def load_skills(self, skill_ids: list[str]) -> dict[str, Any]:
        """Load complete SKILL.md files by exact id, preserving order and deduplicating."""

        selected_ids = _unique_preserve_order(skill_ids)
        if not selected_ids:
            raise SkillRuntimeError("At least one skill_id is required")
        if len(selected_ids) > DEFAULT_MAX_SKILLS:
            raise SkillRuntimeError(
                f"At most {DEFAULT_MAX_SKILLS} skills can be loaded in one request"
            )

        # Validate the complete request before disclosing any Skill.
        skills = [self._get_skill(skill_id) for skill_id in selected_ids]
        loaded: list[dict[str, Any]] = []
        for skill in skills:
            manifest_path = skill.root / skill.entrypoint
            raw_content = manifest_path.read_text(encoding="utf-8", errors="strict")
            context = (
                "<skill>\n"
                f"<name>{skill.name}</name>\n"
                f"<path>{skill.source_path}</path>\n"
                f"{raw_content.rstrip()}\n"
                "</skill>"
            )
            loaded.append(
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "description": skill.description,
                    "source_path": skill.source_path,
                    "content": context,
                    "content_hash": skill.content_hash,
                    "referenced_paths": list(skill.referenced_paths),
                }
            )
        return {"skills": loaded, "loaded_skill_ids": selected_ids}

    def read(
        self,
        skill_id: str,
        path: str,
        start_line: int = 1,
        max_lines: int = 2000,
        max_chars: int = 32_000,
    ) -> dict[str, Any]:
        """Read an exact Skill-relative file with safe continuation information."""

        skill = self._get_skill(skill_id)
        file_path = self._resolve_path(skill, path)
        if not file_path.exists() or not file_path.is_file():
            raise SkillPathError(f"Skill file not found: {path}")

        lines = file_path.read_text(encoding="utf-8", errors="strict").splitlines()
        if not lines:
            if start_line != 1:
                raise SkillPathError(f"start_line exceeds file length: {start_line}")
            return {
                "skill_id": skill.skill_id,
                "path": path,
                "start_line": 1,
                "end_line": 0,
                "total_lines": 0,
                "content": "",
                "content_hash": _content_hash(file_path),
                "truncated": False,
                "next_start_line": None,
            }

        start = max(1, start_line)
        if start > len(lines):
            raise SkillPathError(f"start_line exceeds file length: {start_line}")

        selected: list[str] = []
        end = start - 1
        char_count = 0
        max_end = min(len(lines), start + max_lines - 1)
        for line_number in range(start, max_end + 1):
            line = lines[line_number - 1]
            if len(line) > max_chars:
                raise SkillLineLimitError(
                    path=path,
                    line_number=line_number,
                    actual_chars=len(line),
                    max_chars=max_chars,
                )
            added = len(line) + (1 if selected else 0)
            if selected and char_count + added > max_chars:
                break
            selected.append(line)
            char_count += added
            end = line_number

        truncated = end < len(lines)
        next_start_line = end + 1 if truncated else None
        if truncated and next_start_line <= start:
            raise SkillRuntimeError("Skill continuation did not advance")
        return {
            "skill_id": skill.skill_id,
            "path": path,
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "content": "\n".join(selected),
            "content_hash": _content_hash(file_path),
            "truncated": truncated,
            "next_start_line": next_start_line,
        }

    def _get_skill(self, skill_id: str) -> Skill:
        safe_id = _safe_skill_id(skill_id)
        try:
            return self._skills[safe_id]
        except KeyError as exc:
            raise SkillNotFoundError(f"Skill not found: {safe_id}") from exc

    def _resolve_path(self, skill: Skill, path: str) -> Path:
        if not path or path.startswith(("/", "\\")):
            raise SkillPathError(f"Unsafe skill path: {path!r}")
        candidate = (skill.root / path).resolve()
        try:
            candidate.relative_to(skill.root)
        except ValueError as exc:
            raise SkillPathError(f"Unsafe skill path: {path!r}") from exc
        return candidate

    def _referenced_paths(
        self,
        skill: Skill,
        text: str,
        *,
        source_path: str,
    ) -> list[str]:
        candidates = list(_BACKTICK_PATH_RE.findall(text))
        for target in _MARKDOWN_LINK_RE.findall(text):
            target = target.split("#", 1)[0].strip()
            if target and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", target):
                candidates.append(target)

        result: list[str] = []
        for candidate in _unique_preserve_order(candidates):
            if "<" in candidate or ">" in candidate:
                raise SkillRuntimeError(
                    f"Invalid Skill reference: skill_id={skill.skill_id!r}, "
                    f"source={source_path!r}, path={candidate!r}"
                )
            try:
                file_path = self._resolve_path(skill, candidate)
            except SkillPathError as exc:
                raise SkillRuntimeError(
                    f"Unsafe Skill reference: skill_id={skill.skill_id!r}, "
                    f"source={source_path!r}, path={candidate!r}"
                ) from exc
            if not file_path.exists():
                raise SkillRuntimeError(
                    f"Missing Skill reference: skill_id={skill.skill_id!r}, "
                    f"source={source_path!r}, path={candidate!r}"
                )
            if not file_path.is_file():
                raise SkillRuntimeError(
                    f"Skill reference is not a file: skill_id={skill.skill_id!r}, "
                    f"source={source_path!r}, path={candidate!r}"
                )
            result.append(candidate)
        return result

    @staticmethod
    def _public_skill_metadata(skill: Skill) -> dict[str, Any]:
        return {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "entrypoint": skill.source_path,
            "content_hash": skill.content_hash,
        }
