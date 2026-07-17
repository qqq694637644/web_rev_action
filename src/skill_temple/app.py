"""FastAPI gateway for exact Skills, atomic browser experiments, and workspace evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .browser_routes import register_browser_actions
from .browser_service import (
    BrowserActionService,
    analysis_workspace_root_from_environment,
    build_browser_service_from_environment,
)
from .runtime import (
    DEFAULT_MAX_SKILLS,
    SkillLineLimitError,
    SkillNotFoundError,
    SkillPathError,
    SkillRuntimeError,
    env_value_from_environment_or_dotenv,
    load_runtime,
)
from .runtime_coordinator import RuntimeCoordinator
from .workspace_routes import register_workspace_actions
from .workspace_service import AnalysisWorkspaceService

BEARER_TOKEN_ENV_VAR = "SKILL_TEMPLE_BEARER_TOKEN"
_PROCESS_GUARDS: dict[str, tuple[BinaryIO, int]] = {}
_PROCESS_GUARD_LOCK = threading.Lock()


def _acquire_single_process_guard(root: Path) -> str:
    resolved = str(root.expanduser().resolve())
    with _PROCESS_GUARD_LOCK:
        current = _PROCESS_GUARDS.get(resolved)
        if current is not None:
            _PROCESS_GUARDS[resolved] = (current[0], current[1] + 1)
            return resolved
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]
        lock_path = Path(tempfile.gettempdir()) / f"web-rev-action-{digest}.lock"
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - Windows is the supported deployment
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError(
                "Another web_rev_action process already owns this analysis workspace. "
                "Run the service with exactly one worker."
            ) from exc
        _PROCESS_GUARDS[resolved] = (handle, 1)
        return resolved


def _release_single_process_guard(key: str) -> None:
    with _PROCESS_GUARD_LOCK:
        current = _PROCESS_GUARDS.get(key)
        if current is None:
            return
        handle, references = current
        if references > 1:
            _PROCESS_GUARDS[key] = (handle, references - 1)
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows is the supported deployment
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            _PROCESS_GUARDS.pop(key, None)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoadSkillsRequest(StrictRequest):
    skill_ids: list[str] = Field(min_length=1, max_length=DEFAULT_MAX_SKILLS)


class ReadSkillContentRequest(StrictRequest):
    skill_id: str = Field(..., description="Exact skill_id from the compiled catalog.")
    path: str = Field(
        ...,
        description="Safe relative path inside the Skill, for example docs/reference.md.",
    )
    start_line: int = Field(default=1, ge=1)
    max_lines: int = Field(default=2000, ge=1, le=5000)


class ErrorDetail(BaseModel):
    code: str
    message: str
    suggested_next_action: str
    line_number: int | None = None
    actual_chars: int | None = None
    max_chars: int | None = None


class StructuredErrorResponse(BaseModel):
    error: ErrorDetail


class LoadedSkill(BaseModel):
    skill_id: str
    name: str
    description: str
    source_path: str
    content: str
    content_hash: str
    referenced_paths: list[str] = Field(default_factory=list)


class LoadSkillsResponse(BaseModel):
    skills: list[LoadedSkill]
    loaded_skill_ids: list[str]


class ReadSkillContentResponse(BaseModel):
    skill_id: str
    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    content_hash: str
    truncated: bool
    next_start_line: int | None = None


def _normalize_server_url(server_url: str | None) -> str | None:
    if server_url is None:
        return None
    normalized = server_url.strip().rstrip("/")
    if not normalized:
        return None
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "server_url must be an absolute http(s) URL, for example https://example.com"
        )
    return normalized


def _first_header_value(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _request_server_url(request: Request) -> str:
    forwarded_proto = _first_header_value(request.headers.get("x-forwarded-proto"))
    forwarded_host = _first_header_value(request.headers.get("x-forwarded-host"))
    forwarded_prefix = _first_header_value(request.headers.get("x-forwarded-prefix")) or ""
    if forwarded_proto and forwarded_host:
        return _normalize_server_url(
            f"{forwarded_proto}://{forwarded_host}{forwarded_prefix}"
        ) or ""
    return _normalize_server_url(str(request.base_url)) or ""


def _normalize_bearer_token(token: str | None) -> str | None:
    if token is None:
        return None
    normalized = token.strip()
    return normalized or None


def _requires_bearer_auth(path: str) -> bool:
    return path.startswith("/v1/")


def _valid_bearer_authorization(authorization: str | None, expected_token: str) -> bool:
    if not authorization:
        return False
    scheme, separator, value = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(value.strip(), expected_token)


def _add_bearer_auth_security(schema: dict[str, Any]) -> dict[str, Any]:
    components = schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["BearerAuth"] = {"type": "http", "scheme": "bearer"}
    for path, path_item in schema.get("paths", {}).items():
        if not _requires_bearer_auth(path):
            continue
        for operation in path_item.values():
            if isinstance(operation, dict):
                operation.setdefault("security", [{"BearerAuth": []}])
    return schema


def create_app(
    skills_dir: str | Path | None = None,
    server_url: str | None = None,
    browser_service: BrowserActionService | None = None,
    workspace_service: AnalysisWorkspaceService | None = None,
) -> FastAPI:
    runtime = load_runtime(skills_dir)
    protocol_skill = next(
        (
            item
            for item in runtime.list_skills()["skills"]
            if item["skill_id"] == "browser-action-protocol"
        ),
        None,
    )
    if protocol_skill is None:
        raise SkillRuntimeError(
            "The configured Skills directory must contain browser-action-protocol."
        )
    protocol_skill_content_hash = str(protocol_skill["content_hash"])
    evidence_root = (
        browser_service.experiments.root
        if browser_service is not None
        else analysis_workspace_root_from_environment()
    )
    configured_server_url = _normalize_server_url(
        server_url or env_value_from_environment_or_dotenv("SKILL_TEMPLE_SERVER_URL")
    )
    bearer_token = _normalize_bearer_token(
        env_value_from_environment_or_dotenv(BEARER_TOKEN_ENV_VAR)
    )

    app = FastAPI(
        title="Web Reverse Action Gateway",
        version="0.5.0",
        description=(
            "Exact Skill loading plus two stable browser-analysis Actions. Browser operation "
            "contracts are progressively disclosed by Skills and strictly validated server-side."
        ),
        openapi_url=None,
        servers=([{"url": configured_server_url}] if configured_server_url else None),
    )
    original_openapi = app.openapi

    def openapi_with_optional_bearer_auth() -> dict[str, Any]:
        schema = original_openapi()
        if bearer_token:
            _add_bearer_auth_security(schema)
        return schema

    app.openapi = openapi_with_optional_bearer_auth  # type: ignore[method-assign]

    @app.middleware("http")
    async def bearer_auth_middleware(request: Request, call_next: Any) -> Any:
        if bearer_token and _requires_bearer_auth(request.url.path):
            if not _valid_bearer_authorization(
                request.headers.get("authorization"), bearer_token
            ):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": {
                            "code": "unauthorized",
                            "message": "Missing or invalid Bearer token.",
                            "suggested_next_action": "configure_bearer_auth",
                        }
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return await call_next(request)

    def structured_error(
        code: str,
        message: str,
        suggested_next_action: str,
    ) -> dict[str, object]:
        return {
            "error": {
                "code": code,
                "message": message,
                "suggested_next_action": suggested_next_action,
            }
        }

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_json(request: Request) -> dict[str, Any]:
        schema = copy.deepcopy(app.openapi())
        if "servers" not in schema:
            schema["servers"] = [{"url": _request_server_url(request)}]
        return schema

    @app.get("/health", include_in_schema=False)
    def health_check() -> dict[str, object]:
        return {"status": "ok", "skills_dir": str(runtime.skills_dir)}

    @app.post(
        "/v1/skills/load",
        operation_id="loadSkills",
        response_model=LoadSkillsResponse,
        responses={
            404: {"model": StructuredErrorResponse},
            422: {"model": StructuredErrorResponse},
        },
        summary="Load complete Skills by exact id.",
        description=(
            "Load up to three exact skill_ids selected from the compiled "
            "Instructions catalog."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def load_skills(request: LoadSkillsRequest) -> LoadSkillsResponse | JSONResponse:
        try:
            result = runtime.load_skills(request.skill_ids)
            return LoadSkillsResponse.model_validate(result)
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            return JSONResponse(status_code=404, content=detail)
        except SkillRuntimeError as exc:
            detail = structured_error("invalid_skill_request", str(exc), "reduce_skill_ids")
            return JSONResponse(status_code=422, content=detail)

    @app.post(
        "/v1/skills/read",
        operation_id="readSkillContent",
        response_model=ReadSkillContentResponse,
        responses={
            404: {"model": StructuredErrorResponse},
            422: {"model": StructuredErrorResponse},
        },
        summary="Read a Skill file by exact safe relative path.",
        description=(
            "Read one exact safe file path inside the selected Skill directory. "
            "Use referenced_paths as recommended entry points, not as an access allowlist."
        ),
        openapi_extra={"x-openai-isConsequential": False},
    )
    def read_skill_content(
        request: ReadSkillContentRequest,
    ) -> ReadSkillContentResponse | JSONResponse:
        try:
            result = runtime.read(
                skill_id=request.skill_id,
                path=request.path,
                start_line=request.start_line,
                max_lines=request.max_lines,
            )
            return ReadSkillContentResponse.model_validate(result)
        except SkillNotFoundError as exc:
            detail = structured_error("skill_not_found", str(exc), "check_skill_id")
            return JSONResponse(status_code=404, content=detail)
        except SkillLineLimitError as exc:
            detail = {
                "error": {
                    "code": "skill_line_exceeds_limit",
                    "message": str(exc),
                    "suggested_next_action": (
                        "Use Workspace tools for this file or split the oversized line."
                    ),
                    "line_number": exc.line_number,
                    "actual_chars": exc.actual_chars,
                    "max_chars": exc.max_chars,
                }
            }
            return JSONResponse(status_code=422, content=detail)
        except SkillPathError as exc:
            detail = structured_error("unsafe_or_missing_path", str(exc), "check_path")
            return JSONResponse(status_code=404, content=detail)

    guard_key: str | None = None
    coordinator = (
        browser_service.coordinator if browser_service is not None else RuntimeCoordinator()
    )
    if browser_service is None:
        guard_key = _acquire_single_process_guard(evidence_root)
        app.state.single_process_guard_key = guard_key
        try:
            resolved_browser_service = build_browser_service_from_environment(
                evidence_root=evidence_root,
                coordinator=coordinator,
            )
        except Exception:
            _release_single_process_guard(guard_key)
            raise
    else:
        resolved_browser_service = browser_service
    register_browser_actions(
        app,
        resolved_browser_service,
        protocol_skill_content_hash=protocol_skill_content_hash,
    )
    resolved_workspace_service = workspace_service or AnalysisWorkspaceService(
        resolved_browser_service.experiments.root,
        shell=(env_value_from_environment_or_dotenv("WEB_REV_WORKSPACE_SHELL") or "pwsh"),
        allow_network=(
            env_value_from_environment_or_dotenv("WEB_REV_WORKSPACE_ALLOW_NETWORK") or "false"
        ).lower()
        in {"1", "true", "yes", "on"},
        coordinator=coordinator,
    )
    register_workspace_actions(app, resolved_workspace_service)

    async def close_browser_service() -> None:
        try:
            await resolved_browser_service.close()
        finally:
            if guard_key is not None:
                _release_single_process_guard(guard_key)

    app.router.add_event_handler("shutdown", close_browser_service)
    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Skill Temple GPT Action gateway.")
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--server-url",
        default=None,
        help="Public absolute http(s) URL to publish in OpenAPI servers.",
    )
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(
        create_app(args.skills_dir, server_url=args.server_url),
        host=args.host,
        port=args.port,
        workers=1,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
