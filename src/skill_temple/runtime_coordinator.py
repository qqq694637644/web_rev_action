"""In-process coordination for the shared browser runtime and protected workspace mutations."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class RuntimeOwner:
    kind: Literal["browser", "workspace"]
    owner_id: str
    operation: str
    session_id: str | None = None
    experiment_id: str | None = None


class RuntimeReservationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RuntimeCoordinator:
    """Atomically reserves the one shared browser or a protected workspace mutation."""

    def __init__(self) -> None:
        self._state_lock = asyncio.Lock()
        self._browser_owner: RuntimeOwner | None = None
        self._workspace_owner: RuntimeOwner | None = None

    @property
    def browser_owner(self) -> RuntimeOwner | None:
        return self._browser_owner

    @property
    def workspace_owner(self) -> RuntimeOwner | None:
        return self._workspace_owner

    async def reserve_browser(self, owner: RuntimeOwner) -> None:
        async with self._state_lock:
            if self._browser_owner is not None:
                current = self._browser_owner
                code = (
                    "session_busy"
                    if owner.session_id
                    and current.session_id
                    and owner.session_id == current.session_id
                    else "browser_busy"
                )
                raise RuntimeReservationError(
                    code,
                    "The shared browser is already reserved by "
                    f"{current.operation} ({current.owner_id}).",
                )
            if self._workspace_owner is not None:
                current = self._workspace_owner
                raise RuntimeReservationError(
                    "workspace_busy",
                    "A protected workspace mutation is active: "
                    f"{current.operation} ({current.owner_id}).",
                )
            self._browser_owner = owner

    async def release_browser(self, owner_id: str) -> None:
        async with self._state_lock:
            if self._browser_owner and self._browser_owner.owner_id == owner_id:
                self._browser_owner = None

    async def reserve_workspace(self, owner: RuntimeOwner) -> None:
        async with self._state_lock:
            if self._browser_owner is not None:
                current = self._browser_owner
                raise RuntimeReservationError(
                    "browser_busy",
                    "A browser operation is active: "
                    f"{current.operation} ({current.owner_id}).",
                )
            if self._workspace_owner is not None:
                current = self._workspace_owner
                raise RuntimeReservationError(
                    "workspace_busy",
                    "Another protected workspace mutation is active: "
                    f"{current.operation} ({current.owner_id}).",
                )
            self._workspace_owner = owner

    async def release_workspace(self, owner_id: str) -> None:
        async with self._state_lock:
            if self._workspace_owner and self._workspace_owner.owner_id == owner_id:
                self._workspace_owner = None

    @asynccontextmanager
    async def workspace_mutation(
        self,
        *,
        owner_id: str,
        operation: str,
    ) -> AsyncIterator[None]:
        owner = RuntimeOwner(
            kind="workspace",
            owner_id=owner_id,
            operation=operation,
        )
        await self.reserve_workspace(owner)
        try:
            yield
        finally:
            await self.release_workspace(owner_id)
