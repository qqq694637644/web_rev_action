from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WorkspaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkspaceChangedFile(WorkspaceModel):
    path: str
    status: Literal["added", "modified", "deleted", "unchanged"]
    previous_sha256: str | None = None
    new_sha256: str | None = None
    previous_bytes: int | None = None
    new_bytes: int | None = None


class WorkspaceExecPwshRequest(WorkspaceModel):
    script: str = Field(min_length=1, max_length=20_000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=1_800)
    max_output_bytes: int | None = Field(default=None, ge=1, le=4_000_000)
    allow_network: bool = False
    plain_output: bool = False
    utf8_output: bool = True


class WorkspaceExecPwshResponse(WorkspaceModel):
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool
    duration_ms: int


class WorkspaceTreeEntry(WorkspaceModel):
    path: str
    type: Literal["file", "dir"]
    depth: int
    bytes: int | None = None


class WorkspaceFileContent(WorkspaceModel):
    path: str
    start_line: int
    end_line: int | None = None
    total_lines: int | None = None
    bytes: int | None = None
    sha256: str | None = None
    content: str = ""
    truncated: bool = False
    error: str | None = None


class WorkspaceReadFilesRequest(WorkspaceModel):
    paths: list[str] = Field(min_length=1, max_length=50)
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=200, ge=1, le=5_000)
    max_bytes_per_file: int | None = Field(default=None, ge=1, le=4_000_000)
    max_bytes: int | None = Field(default=None, ge=1_024, le=8_000_000)


class WorkspaceReadFilesResponse(WorkspaceModel):
    workspace_id: Literal["analysis"] = "analysis"
    files: list[WorkspaceFileContent]
    truncated: bool = False


class WorkspaceSearchMatch(WorkspaceModel):
    path: str
    line_number: int
    column: int | None = None
    line: str
    snippet: str | None = None


class WorkspaceSearchRequest(WorkspaceModel):
    query: str = Field(min_length=1, max_length=500)
    regex: bool = False
    case_sensitive: bool = False
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    context_lines: int = Field(default=2, ge=0, le=20)
    max_matches: int = Field(default=100, ge=1, le=1_000)
    max_bytes: int | None = Field(default=None, ge=1_024, le=8_000_000)


class WorkspaceSearchResponse(WorkspaceModel):
    workspace_id: Literal["analysis"] = "analysis"
    query: str
    engine: Literal["ripgrep"] = "ripgrep"
    matches: list[WorkspaceSearchMatch]
    match_count: int
    truncated: bool = False


class WorkspaceInspectRequest(WorkspaceModel):
    paths: list[str] = Field(default_factory=lambda: ["."], min_length=1, max_length=50)
    queries: list[str] = Field(default_factory=list, max_length=10)
    max_depth: int = Field(default=2, ge=1, le=10)
    max_tree_entries: int = Field(default=200, ge=1, le=5_000)
    context_lines: int = Field(default=2, ge=0, le=20)
    max_search_matches: int = Field(default=50, ge=1, le=1_000)
    max_read_files: int = Field(default=10, ge=0, le=50)
    max_file_lines: int = Field(default=120, ge=1, le=5_000)
    max_bytes_per_file: int | None = Field(default=None, ge=1, le=4_000_000)
    max_bytes: int | None = Field(default=None, ge=1_024, le=8_000_000)


class WorkspaceInspectSearchResult(WorkspaceModel):
    query: str
    engine: Literal["ripgrep"] = "ripgrep"
    matches: list[WorkspaceSearchMatch]
    match_count: int
    truncated: bool = False


class WorkspaceInspectResponse(WorkspaceModel):
    workspace_id: Literal["analysis"] = "analysis"
    tree: list[WorkspaceTreeEntry]
    tree_truncated: bool = False
    searches: list[WorkspaceInspectSearchResult] = Field(default_factory=list)
    files: list[WorkspaceFileContent] = Field(default_factory=list)
    truncated: bool = False


class WorkspaceApplyPatchRequest(WorkspaceModel):
    patch: str = Field(min_length=1, max_length=2_000_000)
    dry_run: bool = False
    allow_delete: bool = False
    max_changed_files: int | None = Field(default=None, ge=1, le=200)
    max_patch_bytes: int | None = Field(default=None, ge=1, le=2_000_000)


class WorkspaceApplyPatchResponse(WorkspaceModel):
    applied: bool
    dry_run: bool
    changed_files: list[WorkspaceChangedFile]
    diff_stat: str


class WorkspaceWriteFileRequest(WorkspaceModel):
    path: str = Field(min_length=1, max_length=500)
    content: str
    mode: Literal["create_only", "overwrite", "overwrite_if_sha256_matches"] = (
        "create_only"
    )
    encoding: Literal["utf-8"] = "utf-8"
    line_ending: Literal["preserve", "lf", "crlf"] = "preserve"
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    dry_run: bool = False
    max_bytes: int | None = Field(default=None, ge=1, le=8_000_000)


class WorkspaceWriteFileResponse(WorkspaceModel):
    written: bool
    dry_run: bool
    path: str
    operation: Literal["added", "modified", "unchanged"]
    previous_sha256: str | None
    new_sha256: str
    bytes: int
    changed_files: list[WorkspaceChangedFile]
    diff_stat: str
