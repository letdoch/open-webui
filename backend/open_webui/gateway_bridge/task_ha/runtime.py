"""Process-wide facade of the task drain and instance liveness feature (the only API the upstream hooks use)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import ClassVar

from fastapi import FastAPI, Request
from redis.asyncio import Redis

from open_webui.env import REDIS_KEY_PREFIX
from open_webui.gateway_bridge.bridge_settings import GatewayBridgeSettings
from open_webui.gateway_bridge.task_ha.liveness import LEGACY_OWNER, InstanceLiveness
from open_webui.gateway_bridge.task_ha.process_runtime import TaskHaRuntime
from open_webui.gateway_bridge.task_ha.task_store import TaskStore

log = logging.getLogger(__name__)


class TaskHa:
    """Hooks for ``tasks.py``, ``main.py`` and the scheduler; inert until ``start`` enabled the feature."""

    _runtime: ClassVar[TaskHaRuntime | None] = None

    @classmethod
    async def start(
        cls,
        app: FastAPI,
        store_factory: Callable[[Redis | None, InstanceLiveness], TaskStore],
    ) -> None:
        """Enable the feature from the bridge settings: identity, heartbeat, reaper, drain hooks.

        Call once in the lifespan startup, after ``app.state.redis`` exists and before the app reports ready:
        the instance key is written here, so tasks of the first beat period are not mistaken for a dead owner's.

        Args:
            app: The application; reads ``gateway_bridge_settings`` and ``redis`` from its state.
            store_factory: Factory constructing the host application's ``TaskStore`` adapter.
        """
        bridge = getattr(app.state, 'gateway_bridge_settings', None)
        if not isinstance(bridge, GatewayBridgeSettings) or not bridge.task_ha.enabled:
            return
        liveness = InstanceLiveness(bridge.task_ha, REDIS_KEY_PREFIX)
        store = store_factory(app.state.redis, liveness)
        runtime = TaskHaRuntime(bridge.task_ha, app.state.redis, liveness, store)
        if runtime.redis is not None:
            try:
                await asyncio.wait_for(runtime.liveness.beat(runtime.redis), bridge.task_ha.instance_heartbeat_seconds)
            except Exception:
                # A Valkey outage must not keep an instance from starting; the periodic beat retries.
                log.warning('Task instance key not written at startup; the heartbeat will retry', exc_info=True)
        runtime.drain.attach(runtime.store)
        runtime.start_background()
        cls._runtime = runtime
        log.info('Task drain and liveness enabled (instance %s)', runtime.liveness.instance_id)

    @classmethod
    async def quiesce(cls) -> None:
        """Finish the drain (wait, cancel at the bound, clear keys), stop the loops, withdraw the instance key.

        Call first in the lifespan shutdown.
        """
        runtime = cls._runtime
        if runtime is None:
            return
        await runtime.drain.quiesce()
        for task in runtime.background:
            task.cancel()
        if runtime.redis is not None:
            try:
                await runtime.liveness.withdraw(runtime.redis)
            except Exception:
                log.exception('Could not withdraw the task instance key')

    @classmethod
    def enabled(cls) -> bool:
        """Whether the feature is enabled in this process.

        Returns:
            ``True`` once ``start`` activated task HA, otherwise ``False``.
        """
        return cls._runtime is not None

    @classmethod
    def is_draining(cls) -> bool:
        """Whether this instance is draining: readiness false, no scheduler claims, no new external chats.

        Returns:
            ``True`` while this process is draining, otherwise ``False``.
        """
        return cls._runtime is not None and cls._runtime.drain.draining

    @classmethod
    def admit(cls, request: Request) -> None:
        """Refuse an external chat request with a 503 while draining; no-op when disabled.

        Args:
            request: The chat completion request.
        """
        if cls._runtime is not None:
            cls._runtime.drain.admit(request)

    @classmethod
    def cancel_error(cls) -> str | None:
        """Return the drain error text if the current task is cancelled by the drain, else ``None``.

        Returns:
            Drain error text when the active task was cancelled by drain, otherwise ``None``.
        """
        return cls._runtime.drain.cancel_error() if cls._runtime is not None else None

    @classmethod
    def owner_token(cls) -> str:
        """Return the value to store in a task key: this instance's id, or the legacy marker when disabled.

        Returns:
            The instance id when task HA is active, otherwise ``LEGACY_OWNER``.
        """
        return cls._runtime.liveness.instance_id if cls._runtime is not None else LEGACY_OWNER

    @classmethod
    async def list_tasks(cls, redis: Redis, item_id: str | None) -> list[str]:
        """Return the active task ids of ``item_id`` (all tasks when ``None``), hiding dead owners' tasks.

        Args:
            redis: The shared Valkey client.
            item_id: Chat id, or ``None`` for all tasks.

        Returns:
            The active task ids.

        Raises:
            RuntimeError: When called while the feature is disabled (callers check ``enabled`` first).
        """
        if cls._runtime is None:
            raise RuntimeError('task_ha is not enabled')
        return await cls._runtime.store.list_tasks(redis, item_id)
