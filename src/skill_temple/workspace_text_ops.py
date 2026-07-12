from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

PatchKind = Literal["update", "add", "delete"]

_BINARY_PATCH_MARKERS = ("GIT binary patch", "Binary files ", "Binary file ")


class WorkspaceToolError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class TextPatchHunk:
    old_lines: list[str]
    new_lines: list[str]


@dataclass(frozen=True)
class TextPatchOperation:
    kind: PatchKind
    path: str
    hunks: list[TextPatchHunk] = field(default_factory=list)
    add_lines: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FileSnapshot:
    path: str
    resolved_path: Path
    existed: bool
    data: bytes | None


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def assert_payload_size(data: bytes, *, max_bytes: int, label: str) -> None:
    if len(data) > max_bytes:
        raise WorkspaceToolError(
            "workspace_payload_too_large",
            f"{label} is too large: {len(data)} bytes > {max_bytes} bytes.",
            413,
        )


def assert_text_bytes(data: bytes, *, path: str | None = None) -> None:
    if b"\x00" in data:
        raise WorkspaceToolError(
            "workspace_binary_not_allowed",
            "NUL bytes are not allowed in workspace text operations.",
            403,
        )
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceToolError(
            "workspace_binary_not_allowed",
            f"Only UTF-8 text files are allowed: {path or '<payload>'}.",
            403,
        ) from exc


def normalize_workspace_path(path: str) -> str:
    raw = path.replace("\\", "/").strip()
    if raw in {"", "."}:
        return "."
    pure = PurePosixPath(raw)
    if pure.is_absolute() or ".." in pure.parts:
        raise WorkspaceToolError(
            "workspace_invalid_path",
            "Workspace paths must be relative and cannot contain '..'.",
            400,
        )
    return pure.as_posix().strip("/")


def resolve_workspace_path(root: Path, path: str, *, require_file: bool = False) -> Path:
    normalized = normalize_workspace_path(path)
    resolved_root = root.resolve()
    candidate = resolved_root if normalized == "." else resolved_root / normalized
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise WorkspaceToolError(
            "workspace_path_not_allowed",
            "Resolved path escapes the analysis workspace.",
            403,
        ) from exc
    if require_file and not resolved.is_file():
        raise WorkspaceToolError(
            "workspace_file_not_found",
            f"Workspace file was not found: {normalized}",
            404,
        )
    return resolved


def snapshot_files(root: Path, paths: list[str]) -> list[FileSnapshot]:
    snapshots: list[FileSnapshot] = []
    seen: set[str] = set()
    for raw_path in paths:
        path = normalize_workspace_path(raw_path)
        if path in seen:
            continue
        seen.add(path)
        resolved = resolve_workspace_path(root, path)
        if resolved.exists():
            if not resolved.is_file():
                raise WorkspaceToolError(
                    "workspace_policy_violation",
                    "Workspace text operations only support files.",
                    403,
                )
            snapshots.append(
                FileSnapshot(
                    path=path,
                    resolved_path=resolved,
                    existed=True,
                    data=resolved.read_bytes(),
                )
            )
        else:
            snapshots.append(
                FileSnapshot(path=path, resolved_path=resolved, existed=False, data=None)
            )
    return snapshots


def restore_files(root: Path, snapshots: list[FileSnapshot]) -> None:
    resolved_root = root.resolve()
    for snapshot in snapshots:
        if snapshot.existed:
            snapshot.resolved_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot.resolved_path.write_bytes(snapshot.data or b"")
        elif snapshot.resolved_path.exists() and snapshot.resolved_path.is_file():
            snapshot.resolved_path.unlink()
            _remove_empty_parents(snapshot.resolved_path.parent, resolved_root)


def _remove_empty_parents(path: Path, stop_at: Path) -> None:
    current = path.resolve(strict=False)
    while current != stop_at:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def parse_codex_patch(
    patch: str,
    root: Path,
    *,
    allow_delete: bool,
    max_changed_files: int,
) -> list[TextPatchOperation]:
    payload = patch.encode("utf-8")
    assert_text_bytes(payload)
    if any(marker in patch for marker in _BINARY_PATCH_MARKERS):
        raise WorkspaceToolError(
            "workspace_binary_not_allowed", "Binary patches are not allowed.", 403
        )
    lines = patch.splitlines()
    if (
        not lines
        or lines[0].strip() != "*** Begin Patch"
        or lines[-1].strip() != "*** End Patch"
    ):
        raise WorkspaceToolError(
            "workspace_patch_invalid",
            "Patch must start with '*** Begin Patch' and end with '*** End Patch'.",
            400,
        )

    operations: list[TextPatchOperation] = []
    paths_seen: set[str] = set()
    idx = 1
    while idx < len(lines) - 1:
        line = lines[idx]
        if not line.strip():
            idx += 1
            continue
        if line.startswith("*** Update File: "):
            path = normalize_workspace_path(
                line.removeprefix("*** Update File: ").strip()
            )
            resolved = resolve_workspace_path(root, path)
            if not resolved.is_file():
                raise WorkspaceToolError(
                    "workspace_patch_invalid",
                    f"Update File target does not exist: {path}",
                    400,
                )
            body, idx = _collect_operation_body(lines, idx + 1)
            operations.append(
                TextPatchOperation(
                    kind="update", path=path, hunks=_parse_update_hunks(body, path)
                )
            )
        elif line.startswith("*** Add File: "):
            path = normalize_workspace_path(
                line.removeprefix("*** Add File: ").strip()
            )
            if resolve_workspace_path(root, path).exists():
                raise WorkspaceToolError(
                    "workspace_patch_invalid",
                    f"Add File target already exists: {path}",
                    409,
                )
            body, idx = _collect_operation_body(lines, idx + 1)
            operations.append(
                TextPatchOperation(
                    kind="add", path=path, add_lines=_parse_add_file_lines(body, path)
                )
            )
        elif line.startswith("*** Delete File: "):
            path = normalize_workspace_path(
                line.removeprefix("*** Delete File: ").strip()
            )
            if not allow_delete:
                raise WorkspaceToolError(
                    "workspace_delete_not_allowed",
                    "Delete File is disabled for this request.",
                    403,
                )
            if not resolve_workspace_path(root, path).is_file():
                raise WorkspaceToolError(
                    "workspace_patch_invalid",
                    f"Delete File target does not exist: {path}",
                    400,
                )
            body, idx = _collect_operation_body(lines, idx + 1)
            if any(item.strip() for item in body):
                raise WorkspaceToolError(
                    "workspace_patch_invalid",
                    "Delete File sections cannot contain file content.",
                    400,
                )
            operations.append(TextPatchOperation(kind="delete", path=path))
        else:
            raise WorkspaceToolError(
                "workspace_patch_invalid",
                f"Unsupported patch operation: {line}",
                400,
            )
        paths_seen.add(operations[-1].path)
        if len(paths_seen) > max_changed_files:
            raise WorkspaceToolError(
                "workspace_too_many_changed_files",
                f"Patch changes too many files: {len(paths_seen)} > {max_changed_files}.",
                413,
            )
    if not operations:
        raise WorkspaceToolError(
            "workspace_patch_invalid", "Patch has no file operations.", 400
        )
    return operations


def apply_text_patch(root: Path, operations: list[TextPatchOperation]) -> list[str]:
    changed_paths: list[str] = []
    for operation in operations:
        file_path = resolve_workspace_path(root, operation.path)
        if operation.kind == "add":
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(
                _join_lines(
                    operation.add_lines, trailing_newline=bool(operation.add_lines)
                ),
                encoding="utf-8",
                newline="",
            )
        elif operation.kind == "delete":
            file_path.unlink()
        else:
            original = file_path.read_bytes()
            assert_text_bytes(original, path=operation.path)
            original_text = (
                original.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            )
            lines, trailing = _split_text_lines(original_text)
            new_lines = _apply_hunks(lines, operation.hunks, operation.path)
            file_path.write_text(
                _join_lines(new_lines, trailing_newline=trailing),
                encoding="utf-8",
                newline="",
            )
        changed_paths.append(operation.path)
    return changed_paths


def _collect_operation_body(lines: list[str], start: int) -> tuple[list[str], int]:
    end = start
    markers = ("*** Update File: ", "*** Add File: ", "*** Delete File: ")
    while end < len(lines) - 1 and not lines[end].startswith(markers):
        end += 1
    return lines[start:end], end


def _parse_add_file_lines(body: list[str], path: str) -> list[str]:
    output: list[str] = []
    for line in body:
        if line == "":
            continue
        if not line.startswith("+"):
            raise WorkspaceToolError(
                "workspace_patch_invalid",
                f"Add File content lines must start with '+': {path}",
                400,
            )
        output.append(line[1:])
    return output


def _parse_update_hunks(body: list[str], path: str) -> list[TextPatchHunk]:
    hunks: list[TextPatchHunk] = []
    current: list[str] | None = None
    for line in body:
        if line.startswith("@@"):
            if current is not None:
                hunks.append(_build_hunk(current, path))
            current = []
            continue
        if current is None:
            if not line.strip():
                continue
            raise WorkspaceToolError(
                "workspace_patch_invalid",
                f"Update File requires '@@' hunks: {path}",
                400,
            )
        if line.startswith("\\ No newline at end of file"):
            continue
        if line == "" or line[0] not in {" ", "+", "-"}:
            raise WorkspaceToolError(
                "workspace_patch_invalid",
                f"Patch hunk lines must start with space, '+' or '-': {path}",
                400,
            )
        current.append(line)
    if current is not None:
        hunks.append(_build_hunk(current, path))
    if not hunks:
        raise WorkspaceToolError(
            "workspace_patch_invalid", f"Update has no hunks: {path}", 400
        )
    return hunks


def _build_hunk(lines: list[str], path: str) -> TextPatchHunk:
    old_lines: list[str] = []
    new_lines: list[str] = []
    for line in lines:
        marker, value = line[0], line[1:]
        if marker == " ":
            old_lines.append(value)
            new_lines.append(value)
        elif marker == "-":
            old_lines.append(value)
        elif marker == "+":
            new_lines.append(value)
    if not old_lines and not new_lines:
        raise WorkspaceToolError(
            "workspace_patch_invalid", f"Empty patch hunk: {path}", 400
        )
    return TextPatchHunk(old_lines=old_lines, new_lines=new_lines)


def _apply_hunks(
    lines: list[str], hunks: list[TextPatchHunk], path: str
) -> list[str]:
    current = list(lines)
    cursor = 0
    for hunk in hunks:
        if hunk.old_lines:
            idx = _find_subsequence(current, hunk.old_lines, cursor)
            if idx < 0 and cursor > 0:
                idx = _find_subsequence(current, hunk.old_lines, 0)
            if idx < 0:
                raise WorkspaceToolError(
                    "workspace_patch_context_mismatch",
                    f"Patch context did not match: {path}",
                    409,
                )
            current = current[:idx] + hunk.new_lines + current[idx + len(hunk.old_lines) :]
            cursor = idx + len(hunk.new_lines)
        else:
            current = current[:cursor] + hunk.new_lines + current[cursor:]
            cursor += len(hunk.new_lines)
    return current


def _find_subsequence(lines: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return start
    last_start = len(lines) - len(needle)
    for idx in range(max(start, 0), last_start + 1):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    return -1


def _split_text_lines(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], False
    parts = text.split("\n")
    trailing = parts[-1] == ""
    if trailing:
        parts = parts[:-1]
    return parts, trailing


def _join_lines(lines: list[str], *, trailing_newline: bool) -> str:
    text = "\n".join(lines)
    if trailing_newline:
        text += "\n"
    return text


def normalize_line_endings(
    content: str, *, line_ending: str, previous_bytes: bytes | None
) -> str:
    if line_ending == "preserve":
        if (
            previous_bytes
            and b"\r\n" in previous_bytes
            and previous_bytes.count(b"\r\n") >= previous_bytes.count(b"\n")
        ):
            line_ending = "crlf"
        else:
            return content
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if line_ending == "lf":
        return normalized
    if line_ending == "crlf":
        return normalized.replace("\n", "\r\n")
    raise WorkspaceToolError(
        "workspace_invalid_line_ending", f"Unsupported line ending: {line_ending}", 422
    )
