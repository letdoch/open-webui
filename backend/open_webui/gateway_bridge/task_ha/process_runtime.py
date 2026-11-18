"""Process-scoped runtime state wiring drain, liveness, store, and reaper loops."""

from __future__ import annotations

import asyncio

from redis.asyncio import Redis

from open_webui.gateway_bridge.task_ha.drain import TaskDrain
from open_webui.gateway_bridge.task_ha.liveness import InstanceLiveness, periodic
from open_webui.gateway_bridge.task_ha.reaper import DeadOwnerReaper
from open_webui.gateway_bridge.task_ha.task_store import TaskStore
from open_webui.gateway_bridge.task_ha_settings import TaskHaSettings


class TaskHaRuntime:
    """The enabled task drain and instance liveness runtime of this process."""

    def __init__(
        self,
        settings: TaskHaSettings,
        redis: Redis | None,
        liveness: InstanceLiveness,
        store: TaskStore,
    ) -> None:
        """Assemble the runtime collaborators for an enabled process.

        Args:
            settings: Validated ``task_ha`` settings.
            redis: Shared Valkey client, or ``None`` when Valkey is not configured.
            liveness: Identity and heartbeat helper for this process.
            store: Host application task store adapter.
        """
        self.settings = settings
        self.redis = redis
        self.drain = TaskDrain(settings)
        self.liveness = liveness
        self.store = store
        self.reaper = DeadOwnerReaper(liveness, store)
        self.background: list[asyncio.Task] = []

    async def heartbeat(self) -> None:
        """Run one heartbeat: refresh the instance key, then restore missing registrations of local tasks."""
        if self.redis is not None:
            await self.liveness.beat(self.redis)
            await self.store.reregister()

    async def sweep(self) -> None:
        """Run one dead-owner reaper sweep when Valkey is configured."""
        if self.redis is not None:
            await self.reaper.sweep(self.redis)

    def start_background(self) -> None:
        """Start the heartbeat and reaper loops (with Valkey) and the drain-file watcher (when configured)."""
        beat = self.settings.instance_heartbeat_seconds
        if self.redis is not None:
            self.background.append(asyncio.create_task(periodic('Task instance heartbeat', beat, beat, self.heartbeat)))
            self.background.append(
                asyncio.create_task(periodic('Task reaper', beat, self.settings.instance_ttl_seconds, self.sweep))
            )
        if self.settings.drain_file:
            self.background.append(asyncio.create_task(self.drain.watch_drain_file()))
