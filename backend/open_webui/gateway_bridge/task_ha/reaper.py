"""Reaper: ends the tasks of dead instances (finalize the message, tell the client, clear the keys)."""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from open_webui.gateway_bridge.task_ha.liveness import InstanceLiveness
from open_webui.gateway_bridge.task_ha.orphan_store import OrphanStore

log = logging.getLogger(__name__)


class DeadOwnerReaper:
    """Finds tasks whose owner instance is dead and hands them to the store, once.

    An owner must be seen dead in two consecutive sweeps before its tasks are reaped, so a beat that was merely late
    (the owner then re-registers its tasks) never ends a live chat.
    """

    def __init__(self, liveness: InstanceLiveness, store: OrphanStore) -> None:
        """Create the reaper.

        Args:
            liveness: Identity and dead-owner check.
            store: The host application's task storage.
        """
        self._liveness = liveness
        self._store = store
        self._suspects: frozenset[str] = frozenset()

    async def sweep(self, redis: Redis) -> int:
        """Run one sweep.

        Args:
            redis: The shared Valkey client.

        Returns:
            The number of tasks reaped by this instance.
        """
        items = await self._store.all_task_items(redis)
        task_ids = list(items)
        owners = await self._store.owners_of(redis, task_ids) if task_ids else []
        dead = await self._liveness.dead_owners(redis, owners)
        confirmed = dead & self._suspects
        self._suspects = dead
        reaped = 0
        for task_id, owner in zip(task_ids, owners):
            if owner not in confirmed or not await self._store.claim(redis, task_id):
                continue
            await self._store.finish_orphan(redis, task_id, items[task_id] or None)
            reaped += 1
        if reaped:
            log.warning('Reaped %d task(s) of dead instance(s) %s', reaped, sorted(confirmed))
        return reaped
