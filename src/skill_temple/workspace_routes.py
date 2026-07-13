from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .workspace_models import (
    WorkspaceApplyPatchRequest,
    WorkspaceApplyPatchResponse,
    WorkspaceExecPwshRequest,
    WorkspaceExecPwshResponse,
    WorkspaceInspectRequest,
    WorkspaceInspectResponse,
    WorkspaceReadFilesRequest,
    WorkspaceReadFilesResponse,
    WorkspaceSearchRequest,
    WorkspaceSearchResponse,
    WorkspaceWriteFileRequest,
    WorkspaceWriteFileResponse,
)
from .workspace_service import AnalysisWorkspaceService
from .workspace_text_ops import WorkspaceToolError


def register_workspace_actions(app: FastAPI, service: AnalysisWorkspaceService) -> None:
    app.state.analysis_workspace_service = service

    def raise_http(exc: WorkspaceToolError) -> None:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "suggested_next_action": "inspect_workspace_or_adjust_request",
                }
            },
        ) from exc

    @app.post(
        "/v1/workspace/inspect",
        operation_id="workspaceInspect",
        response_model=WorkspaceInspectResponse,
        response_model_exclude_none=True,
        summary="Inspect analysis workspace tree, search matches, and file snippets.",
        description=(
            "Gateway-compatible local workspace inspection over data/analysis-workspace. "
            "Returns a bounded tree, ripgrep matches, and related UTF-8 file snippets."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_inspect(
        request: WorkspaceInspectRequest,
    ) -> WorkspaceInspectResponse:
        try:
            return await service.inspect(request)
        except WorkspaceToolError as exc:
            raise_http(exc)

    @app.post(
        "/v1/workspace/search",
        operation_id="workspaceSearch",
        response_model=WorkspaceSearchResponse,
        response_model_exclude_none=True,
        summary="Search analysis workspace text with ripgrep.",
        description=(
            "Search UTF-8 text without starting PowerShell. Binary files are skipped by ripgrep; "
            "use workspaceExecPwsh for hashes, byte slices, Base64, compressed data, "
            "or binary parsing."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_search(
        request: WorkspaceSearchRequest,
    ) -> WorkspaceSearchResponse:
        try:
            return await service.search(request)
        except WorkspaceToolError as exc:
            raise_http(exc)

    @app.post(
        "/v1/workspace/read-files",
        operation_id="workspaceReadFiles",
        response_model=WorkspaceReadFilesResponse,
        response_model_exclude_none=True,
        summary="Read multiple UTF-8 analysis files with line numbers.",
        description=(
            "Read bounded UTF-8 text ranges from one or more relative paths. "
            "Use workspaceExecPwsh for non-text artifacts and offset-based binary reads."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    async def workspace_read_files(
        request: WorkspaceReadFilesRequest,
    ) -> WorkspaceReadFilesResponse:
        try:
            return await service.read_files(request)
        except WorkspaceToolError as exc:
            raise_http(exc)

    @app.post(
        "/v1/workspace/write-file",
        operation_id="workspaceWriteFile",
        response_model=WorkspaceWriteFileResponse,
        response_model_exclude_none=True,
        summary="Create or replace one UTF-8 analysis file.",
        description=(
            "Gateway-compatible UTF-8 file creation/replacement with create-only, overwrite, "
            "conditional SHA-256 overwrite, line-ending control, and dry-run support."
        ),
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def workspace_write_file(
        request: WorkspaceWriteFileRequest,
    ) -> WorkspaceWriteFileResponse:
        try:
            return await service.write_file(request)
        except WorkspaceToolError as exc:
            raise_http(exc)

    @app.post(
        "/v1/workspace/apply-patch",
        operation_id="workspaceApplyPatch",
        response_model=WorkspaceApplyPatchResponse,
        response_model_exclude_none=True,
        summary="Apply a controlled Codex text patch in the analysis workspace.",
        description=(
            "Apply Begin Patch/End Patch UTF-8 text operations with dry-run, delete opt-in, "
            "changed-file limits, rollback on failure, and no Git behavior."
        ),
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def workspace_apply_patch(
        request: WorkspaceApplyPatchRequest,
    ) -> WorkspaceApplyPatchResponse:
        try:
            return await service.apply_patch(request)
        except WorkspaceToolError as exc:
            raise_http(exc)

    @app.post(
        "/v1/workspace/exec-pwsh",
        operation_id="workspaceExecPwsh",
        response_model=WorkspaceExecPwshResponse,
        response_model_exclude_none=True,
        summary="Run controlled PowerShell 7 in the analysis workspace.",
        description=(
            "Execute PowerShell 7 from the analysis root with bounded output, timeout, UTF-8 "
            "configuration, optional ANSI stripping, sanitized environment, and "
            "process-tree termination."
        ),
        openapi_extra={"x-openai-isConsequential": True},
    )
    async def workspace_exec_pwsh(
        request: WorkspaceExecPwshRequest,
    ) -> WorkspaceExecPwshResponse:
        try:
            return await service.exec_pwsh(request)
        except WorkspaceToolError as exc:
            raise_http(exc)
