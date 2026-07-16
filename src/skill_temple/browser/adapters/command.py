"""Subprocess command boundary used by browser transports."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

from .contracts import AdapterError, CommandResult, DeadlineLike


class SubprocessCommandRunner:
    def __init__(self, *, max_output_bytes: int = 1_000_000) -> None:
        self.max_output_bytes = max_output_bytes

    @staticmethod
    async def _read_bounded(
        stream: asyncio.StreamReader | None,
        state: dict[str, Any],
        key: str,
    ) -> None:
        if stream is None:
            return
        parts: list[bytes] = state[key]
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                return
            remaining = int(state["remaining"])
            if remaining > 0:
                kept = chunk[:remaining]
                parts.append(kept)
                state["remaining"] = remaining - len(kept)
            if len(chunk) > remaining:
                state["truncated"] = True

    async def _collect_output(
        self,
        process: asyncio.subprocess.Process,
        timeout: float,
    ) -> tuple[bytes, bytes, bool]:
        state: dict[str, Any] = {
            "remaining": self.max_output_bytes,
            "truncated": False,
            "stdout": [],
            "stderr": [],
        }
        readers = [
            asyncio.create_task(self._read_bounded(process.stdout, state, "stdout")),
            asyncio.create_task(self._read_bounded(process.stderr, state, "stderr")),
        ]
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            await self._terminate_tree(process)
            raise
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._terminate_tree(process))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise
        finally:
            await asyncio.gather(*readers, return_exceptions=True)
        return (
            b"".join(state["stdout"]),
            b"".join(state["stderr"]),
            bool(state["truncated"]),
        )

    async def _terminate_tree(self, process: asyncio.subprocess.Process) -> None:
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

    async def run(
        self,
        argv: list[str],
        *,
        deadline: DeadlineLike,
        cwd: Path | None = None,
        allow_failure: bool = False,
    ) -> CommandResult:
        deadline.ensure_remaining("subprocess")
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd) if cwd else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise AdapterError(
                f"Command could not be started: {argv[0]}: {exc}",
                dispatch_started=False,
                outcome_unknown=False,
            ) from exc
        try:
            stdout, stderr, truncated = await self._collect_output(
                process,
                deadline.remaining_seconds(),
            )
        except TimeoutError as exc:
            raise AdapterError(
                f"Command timed out: {argv[0]} {argv[-1]}",
                dispatch_started=True,
                outcome_unknown=True,
            ) from exc
        result = CommandResult(
            argv=argv,
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            truncated=truncated,
        )
        if result.returncode != 0 and not allow_failure:
            message = (result.stderr or result.stdout).strip()[-4000:]
            raise AdapterError(
                f"Command failed ({result.returncode}): {message}",
                dispatch_started=True,
                outcome_unknown=False,
            )
        return result
