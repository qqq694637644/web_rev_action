from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .workspace_models import (
    WorkspaceApplyPatchRequest,
    WorkspaceApplyPatchResponse,
    WorkspaceChangedFile,
    WorkspaceExecPwshRequest,
    WorkspaceExecPwshResponse,
    WorkspaceFileContent,
    WorkspaceInspectRequest,
    WorkspaceInspectResponse,
    WorkspaceInspectSearchResult,
    WorkspaceReadFilesRequest,
    WorkspaceReadFilesResponse,
    WorkspaceSearchMatch,
    WorkspaceSearchRequest,
    WorkspaceSearchResponse,
    WorkspaceTreeEntry,
    WorkspaceWriteFileRequest,
    WorkspaceWriteFileResponse,
)
from .workspace_text_ops import (
    FileSnapshot,
    WorkspaceToolError,
    apply_text_patch,
    assert_payload_size,
    assert_text_bytes,
    normalize_line_endings,
    normalize_workspace_path,
    parse_codex_patch,
    resolve_workspace_path,
    restore_files,
    sha256_hex,
    snapshot_files,
)

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TRUNCATION_MARKER = "\n...[truncated]"
_EXCLUDED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}
_ALLOWED_ENV_READ_FILES = {".env.example", ".env.sample", ".env.template"}
_SEARCH_LINE_MAX_BYTES = 8_192
_SEARCH_SNIPPET_MAX_BYTES = 64_000
_MIN_STRUCTURED_RESPONSE_BYTES = 1_024

_BLOCKED_ALWAYS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bgit\s+push\b", re.IGNORECASE), "git push is not allowed."),
    (re.compile(r"\bgh\s+auth\b", re.IGNORECASE), "GitHub CLI authentication is not allowed."),
    (re.compile(r"\bgh\s+secret\b", re.IGNORECASE), "GitHub secret operations are not allowed."),
    (
        re.compile(r"\bGet-ChildItem\s+Env:", re.IGNORECASE),
        "Environment enumeration is not allowed.",
    ),
    (
        re.compile(r"\bGet-Content\s+\$env:", re.IGNORECASE),
        "Reading environment variables as files is not allowed.",
    ),
    (re.compile(r"\bssh\b", re.IGNORECASE), "ssh is not allowed."),
    (re.compile(r"\bscp\b", re.IGNORECASE), "scp is not allowed."),
]
_NETWORK_BLOCKED: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bInvoke-WebRequest\b", re.IGNORECASE), "Network downloads are disabled."),
    (re.compile(r"\bInvoke-RestMethod\b", re.IGNORECASE), "Network requests are disabled."),
    (re.compile(r"\bcurl\b", re.IGNORECASE), "curl is disabled when network is not allowed."),
    (re.compile(r"\bwget\b", re.IGNORECASE), "wget is disabled when network is not allowed."),
]
_ENV_ALLOWLIST = {
    "PATH",
    "Path",
    "HOME",
    "USERPROFILE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "PSModulePath",
    "TERM",
}


class AnalysisWorkspaceService:
    """Gateway-style text and PowerShell tools over one local analysis directory."""

    def __init__(
        self,
        root: Path,
        *,
        shell: str = "pwsh",
        default_timeout_seconds: int = 120,
        max_timeout_seconds: int = 1_800,
        max_output_bytes: int = 1_000_000,
        allow_network: bool = False,
    ) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.shell = shell
        self.default_timeout_seconds = default_timeout_seconds
        self.max_timeout_seconds = max_timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.allow_network = allow_network
        self._lock = asyncio.Lock()

    async def inspect(self, request: WorkspaceInspectRequest) -> WorkspaceInspectResponse:
        max_file_bytes = self._bounded_output_bytes(request.max_bytes_per_file)
        max_response_bytes = self._bounded_output_bytes(
            request.max_bytes, minimum=_MIN_STRUCTURED_RESPONSE_BYTES
        )
        async with self._lock:
            tree, tree_truncated = self._tree_entries(
                request.paths,
                max_depth=request.max_depth,
                max_entries=request.max_tree_entries,
            )
            searches: list[WorkspaceInspectSearchResult] = []
            related: dict[str, int] = {}
            for query in request.queries:
                search_response = await self._search_workspace(
                    WorkspaceSearchRequest(
                        query=query,
                        paths=request.paths,
                        context_lines=request.context_lines,
                        max_matches=request.max_search_matches,
                        max_bytes=max_response_bytes,
                    )
                )
                searches.append(
                    WorkspaceInspectSearchResult(
                        query=query,
                        matches=search_response.matches,
                        match_count=search_response.match_count,
                        truncated=search_response.truncated,
                    )
                )
                if request.max_read_files > 0:
                    for match in search_response.matches:
                        related.setdefault(match.path, match.line_number)
                        if len(related) >= request.max_read_files:
                            break
            files = [
                self._read_file_content(
                    path,
                    start_line=max(1, first_line - request.context_lines),
                    max_lines=request.max_file_lines,
                    max_bytes=max_file_bytes,
                )
                for path, first_line in list(related.items())[: request.max_read_files]
            ]
        response = WorkspaceInspectResponse(
            tree=tree,
            tree_truncated=tree_truncated,
            searches=searches,
            files=files,
            truncated=(
                tree_truncated
                or any(item.truncated for item in files)
                or any(item.truncated for item in searches)
            ),
        )
        return self._fit_inspect_response(response, max_response_bytes)

    async def search(self, request: WorkspaceSearchRequest) -> WorkspaceSearchResponse:
        async with self._lock:
            return await self._search_workspace(request)

    async def read_files(
        self, request: WorkspaceReadFilesRequest
    ) -> WorkspaceReadFilesResponse:
        max_file_bytes = self._bounded_output_bytes(request.max_bytes_per_file)
        max_response_bytes = self._bounded_output_bytes(
            request.max_bytes, minimum=_MIN_STRUCTURED_RESPONSE_BYTES
        )
        async with self._lock:
            files = [
                self._read_file_content(
                    path,
                    start_line=request.start_line,
                    max_lines=request.max_lines,
                    max_bytes=max_file_bytes,
                )
                for path in request.paths
            ]
        response = WorkspaceReadFilesResponse(
            files=files,
            truncated=any(item.truncated for item in files),
        )
        return self._fit_read_files_response(response, max_response_bytes)

    async def write_file(
        self, request: WorkspaceWriteFileRequest
    ) -> WorkspaceWriteFileResponse:
        max_bytes = min(request.max_bytes or self.max_output_bytes, self.max_output_bytes)
        path = normalize_workspace_path(request.path)
        if path == ".":
            raise WorkspaceToolError(
                "workspace_write_invalid_path", "A file path is required.", 400
            )
        async with self._lock:
            resolved = resolve_workspace_path(self.root, path)
            existed = resolved.exists()
            if existed and not resolved.is_file():
                raise WorkspaceToolError(
                    "workspace_write_invalid_path", "Target is not a file.", 400
                )
            previous = resolved.read_bytes() if existed else None
            if previous is not None:
                assert_text_bytes(previous, path=path)
            previous_sha = sha256_hex(previous) if previous is not None else None
            if request.mode == "create_only" and existed:
                raise WorkspaceToolError(
                    "workspace_write_conflict", "Target already exists.", 409
                )
            if request.mode == "overwrite" and not existed:
                raise WorkspaceToolError(
                    "workspace_file_not_found", "Overwrite target does not exist.", 404
                )
            if request.mode == "overwrite_if_sha256_matches":
                if not existed:
                    raise WorkspaceToolError(
                        "workspace_file_not_found", "Conditional target does not exist.", 404
                    )
                if request.expected_sha256 is None:
                    raise WorkspaceToolError(
                        "workspace_write_conflict",
                        "expected_sha256 is required for conditional overwrite.",
                        400,
                    )
                if previous_sha != request.expected_sha256:
                    raise WorkspaceToolError(
                        "workspace_write_conflict",
                        "Current file SHA-256 does not match expected_sha256.",
                        409,
                    )
            normalized_content = normalize_line_endings(
                request.content,
                line_ending=request.line_ending,
                previous_bytes=previous,
            )
            data = normalized_content.encode("utf-8")
            assert_payload_size(data, max_bytes=max_bytes, label="Workspace file")
            assert_text_bytes(data, path=path)
            new_sha = sha256_hex(data)
            operation = (
                "added"
                if not existed
                else ("unchanged" if data == previous else "modified")
            )
            changed = self._changed_file(path, previous, data)
            if operation != "unchanged" and not request.dry_run:
                resolved.parent.mkdir(parents=True, exist_ok=True)
                temporary = resolved.with_name(f".{resolved.name}.workspace-write.tmp")
                temporary.write_bytes(data)
                os.replace(temporary, resolved)
            return WorkspaceWriteFileResponse(
                written=operation != "unchanged" and not request.dry_run,
                dry_run=request.dry_run,
                path=path,
                operation=operation,
                previous_sha256=previous_sha,
                new_sha256=new_sha,
                bytes=len(data),
                changed_files=[] if operation == "unchanged" else [changed],
                diff_stat=self._diff_stat([] if operation == "unchanged" else [changed]),
            )

    async def apply_patch(
        self, request: WorkspaceApplyPatchRequest
    ) -> WorkspaceApplyPatchResponse:
        patch_bytes = request.patch.encode("utf-8")
        max_patch_bytes = min(
            request.max_patch_bytes or 1_000_000,
            2_000_000,
        )
        assert_payload_size(patch_bytes, max_bytes=max_patch_bytes, label="Patch")
        max_changed_files = min(request.max_changed_files or 50, 200)
        async with self._lock:
            operations = parse_codex_patch(
                request.patch,
                self.root,
                allow_delete=request.allow_delete,
                max_changed_files=max_changed_files,
            )
            paths = [operation.path for operation in operations]
            snapshots = snapshot_files(self.root, paths)
            try:
                apply_text_patch(self.root, operations)
                changed_files = self._changes_from_snapshots(snapshots)
                if request.dry_run:
                    restore_files(self.root, snapshots)
            except Exception:
                restore_files(self.root, snapshots)
                raise
        return WorkspaceApplyPatchResponse(
            applied=not request.dry_run,
            dry_run=request.dry_run,
            changed_files=changed_files,
            diff_stat=self._diff_stat(changed_files),
        )

    async def exec_pwsh(
        self, request: WorkspaceExecPwshRequest
    ) -> WorkspaceExecPwshResponse:
        timeout = min(
            request.timeout_seconds or self.default_timeout_seconds,
            self.max_timeout_seconds,
        )
        max_output = min(
            request.max_output_bytes or self.max_output_bytes,
            self.max_output_bytes,
        )
        self._validate_script(request.script, allow_network=request.allow_network)
        script = self._build_pwsh_script(
            request.script,
            plain_output=request.plain_output,
            utf8_output=request.utf8_output,
        )
        started = time.perf_counter()
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = await asyncio.create_subprocess_exec(
                self.shell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
                cwd=str(self.root),
                env=self._sanitized_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
        except FileNotFoundError as exc:
            raise WorkspaceToolError(
                "workspace_exec_failed",
                f"PowerShell 7 executable was not found: {self.shell}",
                500,
            ) from exc
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError as exc:
            await self._kill_process_tree(process)
            stdout_b, stderr_b = await process.communicate()
            stdout, stderr, truncated = self._decode_output(
                stdout_b,
                stderr_b,
                max_output,
                strip_ansi=request.plain_output,
            )
            raise WorkspaceToolError(
                "workspace_timeout",
                f"PowerShell timed out after {timeout}s. stdout={stdout!r} stderr={stderr!r}",
                408,
            ) from exc
        stdout, stderr, truncated = self._decode_output(
            stdout_b,
            stderr_b,
            max_output,
            strip_ansi=request.plain_output,
        )
        return WorkspaceExecPwshResponse(
            exit_code=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            duration_ms=round((time.perf_counter() - started) * 1000),
        )

    async def _search_workspace(
        self, request: WorkspaceSearchRequest
    ) -> WorkspaceSearchResponse:
        max_bytes = self._bounded_output_bytes(
            request.max_bytes, minimum=_MIN_STRUCTURED_RESPONSE_BYTES
        )
        rg = shutil.which("rg")
        if not rg:
            raise WorkspaceToolError(
                "workspace_exec_failed",
                "ripgrep (rg) is required for workspaceSearch/workspaceInspect.",
                500,
            )
        paths = self._normalize_existing_paths(request.paths)
        args = [rg, "--json", "--line-number", "--column", "--color", "never"]
        if not request.regex:
            args.append("--fixed-strings")
        if not request.case_sensitive:
            args.append("--ignore-case")
        args.extend(["--", request.query, *paths])
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(self.root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await process.communicate()
        stdout_b, stdout_truncated = self._truncate_bytes(stdout_b, max_bytes)
        if process.returncode == 2:
            raise WorkspaceToolError(
                "workspace_search_invalid",
                f"ripgrep rejected the query: {stderr_b.decode('utf-8', errors='replace')}",
                422,
            )
        matches: list[WorkspaceSearchMatch] = []
        truncated = stdout_truncated
        for raw_line in stdout_b.decode("utf-8", errors="replace").splitlines():
            try:
                event = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            raw_path = ((data.get("path") or {}).get("text") or "").replace("\\", "/")
            if self._path_is_excluded(raw_path):
                continue
            line_number = int(data.get("line_number") or 0)
            line_text = str((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
            submatches = data.get("submatches") or []
            column = None
            if submatches and isinstance(submatches[0], dict):
                column = int(submatches[0].get("start") or 0) + 1
            line_text, line_truncated = self._clip_text(line_text, _SEARCH_LINE_MAX_BYTES)
            snippet = self._read_file_content(
                raw_path,
                start_line=max(1, line_number - request.context_lines),
                max_lines=(request.context_lines * 2) + 1,
                max_bytes=min(max_bytes, _SEARCH_SNIPPET_MAX_BYTES),
            )
            truncated = truncated or line_truncated or snippet.truncated
            matches.append(
                WorkspaceSearchMatch(
                    path=raw_path,
                    line_number=line_number,
                    column=column,
                    line=line_text,
                    snippet=snippet.content or None,
                )
            )
            if len(matches) >= request.max_matches:
                truncated = True
                break
        response = WorkspaceSearchResponse(
            query=request.query,
            matches=matches,
            match_count=len(matches),
            truncated=truncated,
        )
        return self._fit_search_response(response, max_bytes)

    def _tree_entries(
        self, paths: list[str], *, max_depth: int, max_entries: int
    ) -> tuple[list[WorkspaceTreeEntry], bool]:
        entries: list[WorkspaceTreeEntry] = []
        for base in self._normalize_existing_paths(paths):
            base_path = self.root if base == "." else self.root / base
            if base_path.is_file():
                entries.append(
                    WorkspaceTreeEntry(
                        path=base,
                        type="file",
                        depth=0,
                        bytes=base_path.stat().st_size,
                    )
                )
                continue
            for current, dirs, files in os.walk(base_path):
                current_path = Path(current)
                rel_current = (
                    self._relative_path(current_path) if current_path != self.root else "."
                )
                depth = 0 if rel_current == "." else len(PurePosixPath(rel_current).parts)
                if depth >= max_depth:
                    dirs[:] = []
                    continue
                dirs[:] = [
                    item
                    for item in sorted(dirs)
                    if not self._path_is_excluded(self._join_path(rel_current, item))
                ]
                for dirname in dirs:
                    rel = self._join_path(rel_current, dirname)
                    entries.append(
                        WorkspaceTreeEntry(
                            path=rel,
                            type="dir",
                            depth=len(PurePosixPath(rel).parts),
                        )
                    )
                    if len(entries) >= max_entries:
                        return entries, True
                for filename in sorted(files):
                    rel = self._join_path(rel_current, filename)
                    if self._path_is_excluded(rel):
                        continue
                    file_path = current_path / filename
                    entries.append(
                        WorkspaceTreeEntry(
                            path=rel,
                            type="file",
                            depth=len(PurePosixPath(rel).parts),
                            bytes=file_path.stat().st_size,
                        )
                    )
                    if len(entries) >= max_entries:
                        return entries, True
        return entries, False

    def _read_file_content(
        self, path: str, *, start_line: int, max_lines: int, max_bytes: int
    ) -> WorkspaceFileContent:
        try:
            normalized = normalize_workspace_path(path)
            if normalized == ".":
                raise WorkspaceToolError(
                    "workspace_write_invalid_path", "A file path is required.", 400
                )
            if self._path_is_excluded(normalized):
                raise WorkspaceToolError(
                    "workspace_path_not_allowed",
                    "Path is excluded from workspace inspection.",
                    403,
                )
            resolved = resolve_workspace_path(self.root, normalized, require_file=True)
            data = resolved.read_bytes()
            assert_text_bytes(data, path=normalized)
            text = data.decode("utf-8")
            lines = text.splitlines()
            start_idx = start_line - 1
            selected = lines[start_idx : start_idx + max_lines]
            output_lines: list[str] = []
            output_bytes = 0
            truncated = start_idx + len(selected) < len(lines)
            for offset, line in enumerate(selected, start=start_line):
                rendered = f"{offset}: {line}"
                rendered_bytes = len((rendered + "\n").encode("utf-8"))
                if rendered_bytes > max_bytes:
                    clipped, _ = self._clip_text(rendered, max_bytes)
                    if clipped:
                        output_lines.append(clipped)
                    truncated = True
                    break
                if output_bytes + rendered_bytes > max_bytes:
                    truncated = True
                    break
                output_lines.append(rendered)
                output_bytes += rendered_bytes
            end_line = start_line + len(output_lines) - 1 if output_lines else None
            content = "\n".join(output_lines)
            content, content_truncated = self._clip_text(content, max_bytes)
            return WorkspaceFileContent(
                path=normalized,
                start_line=start_line,
                end_line=end_line,
                total_lines=len(lines),
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                content=content,
                truncated=truncated or content_truncated,
            )
        except Exception as exc:
            return WorkspaceFileContent(
                path=path,
                start_line=start_line,
                error=str(exc),
                truncated=False,
            )

    def _normalize_existing_paths(self, paths: list[str]) -> list[str]:
        normalized_paths: list[str] = []
        for raw_path in paths:
            normalized = normalize_workspace_path(raw_path)
            if self._path_is_excluded(normalized):
                continue
            resolved = resolve_workspace_path(self.root, normalized)
            if not resolved.exists():
                raise WorkspaceToolError(
                    "workspace_file_not_found",
                    f"Workspace path was not found: {normalized}",
                    404,
                )
            normalized_paths.append(normalized)
        if not normalized_paths:
            raise WorkspaceToolError(
                "workspace_invalid_paths",
                "All requested paths are excluded from workspace inspection.",
                422,
            )
        return normalized_paths

    def _bounded_output_bytes(self, requested: int | None, *, minimum: int = 1) -> int:
        value = min(requested or self.max_output_bytes, self.max_output_bytes)
        if value < minimum:
            raise WorkspaceToolError(
                "workspace_output_too_small",
                "Requested output budget is too small.",
                422,
            )
        return value

    def _fit_read_files_response(
        self, response: WorkspaceReadFilesResponse, max_bytes: int
    ) -> WorkspaceReadFilesResponse:
        while self._model_bytes(response) > max_bytes and response.files:
            files = list(response.files)
            last = files[-1]
            if last.content:
                files[-1] = last.model_copy(update={"content": "", "truncated": True})
            else:
                files.pop()
            response = response.model_copy(update={"files": files, "truncated": True})
        if self._model_bytes(response) > max_bytes:
            raise WorkspaceToolError(
                "workspace_output_too_small", "max_bytes is too small.", 422
            )
        return response

    def _fit_search_response(
        self, response: WorkspaceSearchResponse, max_bytes: int
    ) -> WorkspaceSearchResponse:
        while self._model_bytes(response) > max_bytes and response.matches:
            matches = list(response.matches)
            last = matches[-1]
            if last.snippet:
                matches[-1] = last.model_copy(update={"snippet": None})
            elif last.line:
                matches[-1] = last.model_copy(update={"line": ""})
            else:
                matches.pop()
            response = response.model_copy(
                update={"matches": matches, "match_count": len(matches), "truncated": True}
            )
        if self._model_bytes(response) > max_bytes:
            raise WorkspaceToolError(
                "workspace_output_too_small", "max_bytes is too small.", 422
            )
        return response

    def _fit_inspect_response(
        self, response: WorkspaceInspectResponse, max_bytes: int
    ) -> WorkspaceInspectResponse:
        while self._model_bytes(response) > max_bytes:
            if response.files:
                files = list(response.files)
                last = files[-1]
                if last.content:
                    files[-1] = last.model_copy(update={"content": "", "truncated": True})
                else:
                    files.pop()
                response = response.model_copy(update={"files": files, "truncated": True})
                continue
            if any(search.matches for search in response.searches):
                searches = list(response.searches)
                for index in range(len(searches) - 1, -1, -1):
                    search = searches[index]
                    if not search.matches:
                        continue
                    matches = list(search.matches)
                    last = matches[-1]
                    if last.snippet:
                        matches[-1] = last.model_copy(update={"snippet": None})
                    elif last.line:
                        matches[-1] = last.model_copy(update={"line": ""})
                    else:
                        matches.pop()
                    searches[index] = search.model_copy(
                        update={"matches": matches, "match_count": len(matches), "truncated": True}
                    )
                    break
                response = response.model_copy(update={"searches": searches, "truncated": True})
                continue
            if response.tree:
                response = response.model_copy(
                    update={"tree": response.tree[:-1], "tree_truncated": True, "truncated": True}
                )
                continue
            if response.searches:
                response = response.model_copy(
                    update={
                        "searches": response.searches[:-1],
                        "truncated": True,
                    }
                )
                continue
            raise WorkspaceToolError(
                "workspace_output_too_small", "max_bytes is too small.", 422
            )
        return response

    @staticmethod
    def _model_bytes(model: Any) -> int:
        return len(model.model_dump_json().encode("utf-8"))

    @staticmethod
    def _truncate_bytes(data: bytes, max_bytes: int) -> tuple[bytes, bool]:
        if len(data) <= max_bytes:
            return data, False
        return data[:max_bytes], True

    @staticmethod
    def _clip_text(text: str, max_bytes: int) -> tuple[str, bool]:
        data = text.encode("utf-8", errors="replace")
        if len(data) <= max_bytes:
            return text, False
        marker = _TRUNCATION_MARKER.encode("utf-8")
        if max_bytes <= len(marker):
            return data[:max_bytes].decode("utf-8", errors="ignore"), True
        clipped = data[: max_bytes - len(marker)].decode("utf-8", errors="ignore")
        return clipped + _TRUNCATION_MARKER, True

    def _path_is_excluded(self, path: str) -> bool:
        normalized = path.replace("\\", "/").strip("/")
        if not normalized or normalized == ".":
            return False
        parts = PurePosixPath(normalized).parts
        if any(part in _EXCLUDED_DIRS for part in parts):
            return True
        filename = parts[-1]
        return (
            filename == ".env" or filename.startswith(".env.")
        ) and filename not in _ALLOWED_ENV_READ_FILES

    def _relative_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    @staticmethod
    def _join_path(parent: str, child: str) -> str:
        if parent in {"", "."}:
            return child.replace("\\", "/")
        return f"{parent.rstrip('/')}/{child}".replace("\\", "/")

    @staticmethod
    def _changed_file(
        path: str,
        previous: bytes | None,
        current: bytes | None,
    ) -> WorkspaceChangedFile:
        if previous is None and current is not None:
            status = "added"
        elif previous is not None and current is None:
            status = "deleted"
        elif previous == current:
            status = "unchanged"
        else:
            status = "modified"
        return WorkspaceChangedFile(
            path=path,
            status=status,
            previous_sha256=sha256_hex(previous) if previous is not None else None,
            new_sha256=sha256_hex(current) if current is not None else None,
            previous_bytes=len(previous) if previous is not None else None,
            new_bytes=len(current) if current is not None else None,
        )

    def _changes_from_snapshots(self, snapshots: list[FileSnapshot]) -> list[WorkspaceChangedFile]:
        changed: list[WorkspaceChangedFile] = []
        for snapshot in snapshots:
            current = (
                snapshot.resolved_path.read_bytes()
                if snapshot.resolved_path.is_file()
                else None
            )
            item = self._changed_file(snapshot.path, snapshot.data, current)
            if item.status != "unchanged":
                changed.append(item)
        return changed

    @staticmethod
    def _diff_stat(changed_files: list[WorkspaceChangedFile]) -> str:
        if not changed_files:
            return "0 files changed"
        parts = [f"{item.status}: {item.path}" for item in changed_files]
        return f"{len(changed_files)} file(s) changed\n" + "\n".join(parts)

    def unified_diff(self, path: str, previous: bytes | None, current: bytes | None) -> str:
        before = (previous or b"").decode("utf-8", errors="replace").splitlines()
        after = (current or b"").decode("utf-8", errors="replace").splitlines()
        return "\n".join(
            difflib.unified_diff(before, after, fromfile=f"a/{path}", tofile=f"b/{path}")
        )

    def _validate_script(self, script: str, *, allow_network: bool) -> None:
        for pattern, message in _BLOCKED_ALWAYS:
            if pattern.search(script):
                raise WorkspaceToolError("workspace_script_rejected", message, 403)
        if allow_network and not self.allow_network:
            raise WorkspaceToolError(
                "workspace_script_rejected",
                "Network access is disabled by server configuration.",
                403,
            )
        if not allow_network:
            for pattern, message in _NETWORK_BLOCKED:
                if pattern.search(script):
                    raise WorkspaceToolError("workspace_script_rejected", message, 403)

    @staticmethod
    def _sanitized_environment() -> dict[str, str]:
        clean: dict[str, str] = {}
        for key, value in os.environ.items():
            if key in _ENV_ALLOWLIST or key.upper() in _ENV_ALLOWLIST:
                clean[key] = value
        clean.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "Never",
                "GITHUB_TOKEN": "",
                "GH_TOKEN": "",
                "GPT_ACTION_SECRET": "",
            }
        )
        return clean

    @staticmethod
    def _build_pwsh_script(script: str, *, plain_output: bool, utf8_output: bool) -> str:
        prelude: list[str] = []
        if plain_output:
            prelude.extend(
                [
                    "$ProgressPreference = 'SilentlyContinue'",
                    "if ($PSStyle) { $PSStyle.OutputRendering = 'PlainText' }",
                ]
            )
        if utf8_output:
            prelude.extend(
                [
                    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
                    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false)",
                    "$env:PYTHONIOENCODING = 'utf-8'",
                    "$env:PYTHONUTF8 = '1'",
                    "$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'",
                    "$PSDefaultParameterValues['Set-Content:Encoding'] = 'utf8'",
                    "$PSDefaultParameterValues['Add-Content:Encoding'] = 'utf8'",
                ]
            )
        return script if not prelude else "\n".join([*prelude, script])

    async def _kill_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.communicate()
        else:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    def _decode_output(
        stdout_b: bytes,
        stderr_b: bytes,
        max_bytes: int,
        *,
        strip_ansi: bool,
    ) -> tuple[str, str, bool]:
        total = len(stdout_b) + len(stderr_b)
        truncated = total > max_bytes
        if truncated:
            stdout_limit = max_bytes // 2
            stderr_limit = max_bytes - stdout_limit
            stdout_b = stdout_b[:stdout_limit]
            stderr_b = stderr_b[:stderr_limit]
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if strip_ansi:
            stdout = ANSI_ESCAPE_RE.sub("", stdout)
            stderr = ANSI_ESCAPE_RE.sub("", stderr)
        suffix = _TRUNCATION_MARKER if truncated else ""
        return (
            stdout + (suffix if stdout_b and truncated else ""),
            stderr + (suffix if stderr_b and truncated else ""),
            truncated,
        )
