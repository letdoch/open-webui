"""Protocol describing the process-local task registry used by the drain."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Protocol


class LocalTasks(Protocol):
    """The process-local task registry the drain works on."""

    def local(self) -> Mapping[str, asyncio.Task]:
        """Return the live ``task_id -> task`` mapping.

        Returns:
            Mapping of active local task ids to their asyncio tasks.
        """

    def item_of(self, task_id: str) -> str | None:
        """Return the item (chat) id a task is filed under, if any.

        Args:
            task_id: Identifier of the local task.

        Returns:
            The item id the task belongs to, or ``None`` if unassociated.
        """

    async def cleanup(self, task_id: str, item_id: str | None) -> None:
        """Remove a task from the local registry and from Valkey.

        Args:
            task_id: Identifier of the task to remove.
            item_id: Chat (item) id the task was filed under, if any.
        """
