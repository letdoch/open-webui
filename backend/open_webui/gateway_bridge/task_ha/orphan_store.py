"""Protocol describing host task storage operations needed by the dead-owner reaper."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from redis.asyncio import Redis


class OrphanStore(Protocol):
    """What the reaper needs from the host application's task storage."""

    async def all_task_items(self, redis: Redis) -> Mapping[str, str]:
        """Return every registered ``task_id -> item_id`` (item id empty when the task has none).

        Args:
            redis: The shared Valkey client.

        Returns:
            Mapping from registered task id to its item id string.
        """

    async def owners_of(self, redis: Redis, task_ids: list[str]) -> list[str | None]:
        """Return the owner value of each task key (``None`` when the key is gone).

        Args:
            redis: The shared Valkey client.
            task_ids: Task ids to look up in order.

        Returns:
            Owner token for each task id, or ``None`` when the key expired or was removed.
        """

    async def claim(self, redis: Redis, task_id: str) -> bool:
        """Claim a task for reaping; only one instance wins.

        Args:
            redis: The shared Valkey client.
            task_id: Task id to claim.

        Returns:
            ``True`` if this instance acquired the reap claim, otherwise ``False``.
        """

    async def finish_orphan(self, redis: Redis, task_id: str, item_id: str | None) -> None:
        """Finalize the task's message with an error, emit the events, and clear the task keys.

        Args:
            redis: The shared Valkey client.
            task_id: Identifier of the orphaned task.
            item_id: Chat (item) id the task belongs to, if any.
        """
