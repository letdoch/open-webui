"""Instance identity and heartbeat: lets peers tell a dead owner's task keys from a live one's."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from uuid import uuid4

from redis.asyncio import Redis

from open_webui.gateway_bridge.task_ha_settings import TaskHaSettings

log = logging.getLogger(__name__)

LEGACY_OWNER = '1'
"""Task key value written by instances that predate instance identity: owner unknown, the key TTL decides."""


async def periodic(name: str, period: float, bound: float, work: Callable[[], Awaitable[None]]) -> None:
    """Run ``work`` every ``period`` seconds, each run bounded by ``bound`` seconds; failures are logged.

    A bound is mandatory: a Valkey socket without timeout can block for minutes after a primary loss without reset.

    Args:
        name: Label for log lines.
        period: Seconds between runs.
        bound: Seconds after which a run is abandoned.
        work: Coroutine function to run.
    """
    while True:
        try:
            await asyncio.wait_for(work(), timeout=bound)
        except TimeoutError:
            log.warning('%s did not finish within %ss', name, bound)
        except Exception:
            log.exception('%s failed', name)
        await asyncio.sleep(period)


class InstanceLiveness:
    """One process's identity plus the Valkey key that proves it is alive.

    The key ``{prefix}:tasks:instance:{instance_id}`` carries ``instance_ttl_seconds`` and is refreshed every
    ``instance_heartbeat_seconds``. A task key whose owner key is missing belongs to a dead instance.
    """

    def __init__(self, settings: TaskHaSettings, key_prefix: str) -> None:
        """Draw the instance id; call after any fork so that every worker process is its own instance.

        Args:
            settings: Validated ``task_ha`` settings.
            key_prefix: The Open WebUI Valkey key prefix (``REDIS_KEY_PREFIX``).
        """
        self.settings = settings
        self._prefix = key_prefix
        self.instance_id = uuid4().hex

    def key_for(self, instance_id: str) -> str:
        """Return the liveness key of an instance.

        Args:
            instance_id: Identity of the instance.

        Returns:
            The Valkey key name.
        """
        return f'{self._prefix}:tasks:instance:{instance_id}'

    async def beat(self, redis: Redis) -> None:
        """Refresh this instance's liveness key.

        Args:
            redis: The shared Valkey client.
        """
        await redis.set(self.key_for(self.instance_id), '1', ex=self.settings.instance_ttl_seconds)

    async def withdraw(self, redis: Redis) -> None:
        """Delete the liveness key so that peers treat every remaining key of this instance as dead.

        Args:
            redis: The shared Valkey client.
        """
        await redis.delete(self.key_for(self.instance_id))

    async def dead_owners(self, redis: Redis, owners: Iterable[str | None]) -> frozenset[str]:
        """Return the owners among ``owners`` whose liveness key is gone.

        Own id, unknown (legacy) owners and missing values are never reported: they need no round trip.

        Args:
            redis: The shared Valkey client.
            owners: Owner values read from task keys.

        Returns:
            The instance ids that are dead.
        """
        foreign = sorted({o for o in owners if o and o not in (self.instance_id, LEGACY_OWNER)})
        if not foreign:
            return frozenset()
        pipe = redis.pipeline(transaction=False)
        for owner in foreign:
            pipe.exists(self.key_for(owner))
        found = await pipe.execute()
        return frozenset(owner for owner, exists in zip(foreign, found) if not exists)
