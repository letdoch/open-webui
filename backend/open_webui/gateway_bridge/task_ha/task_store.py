"""Combined storage protocol connecting the task HA runtime to the host application."""

from __future__ import annotations

from typing import Protocol

from redis.asyncio import Redis

from open_webui.gateway_bridge.task_ha.local_tasks import LocalTasks
from open_webui.gateway_bridge.task_ha.orphan_store import OrphanStore


class TaskStore(LocalTasks, OrphanStore, Protocol):
    """Host task storage interface combining local drain, heartbeat reregistration, listing, and reaping."""

    async def reregister(self) -> None:
        """Restore missing Valkey registrations of running local tasks and cancel any already reaped."""

    async def list_tasks(self, redis: Redis, item_id: str | None) -> list[str]:
        """Return active task ids for ``item_id`` (all tasks when ``None``), hiding dead owners' tasks.

        Args:
            redis: The shared Valkey client.
            item_id: Chat (item) id to filter by, or ``None`` for all registered tasks.

        Returns:
            List of active task ids.
        """
