"""Drain of the local task dict: refuse new chats, wait up to a bound, cancel the remainder."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections.abc import Callable
from types import FrameType
from typing import TypeAlias

from fastapi import HTTPException, Request

from open_webui.gateway_bridge.task_ha.local_tasks import LocalTasks
from open_webui.gateway_bridge.task_ha_settings import TaskHaSettings

log = logging.getLogger(__name__)

DRAIN_ERROR_TEXT = 'server_draining: the server stopped before the response was complete; please send it again.'
DRAIN_FILE_POLL_SECONDS = 1.0

_Handler: TypeAlias = Callable[[int, FrameType | None], None] | int | signal.Handlers | None


class TaskDrain:
    """State machine ``serving -> draining -> quiescing`` of one instance."""

    def __init__(self, settings: TaskHaSettings) -> None:
        """Create the drain in the ``serving`` state.

        Args:
            settings: Validated ``task_ha`` settings.
        """
        self._settings = settings
        self._draining = False
        self._terminated_at: float | None = None
        self._cancelled: set[asyncio.Task] = set()
        self._previous: _Handler = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._registry: LocalTasks | None = None
        self._quiesce_task: asyncio.Task | None = None
        self._draining_since: float | None = None

    @property
    def draining(self) -> bool:
        """Whether the instance refuses new chats and reports itself not ready.

        Returns:
            ``True`` once draining has started, otherwise ``False``.
        """
        return self._draining

    def begin_draining(self) -> None:
        """Enter ``draining``; idempotent."""
        if self._draining_since is None:
            self._draining_since = time.monotonic()
        self._draining = True

    def attach(self, registry: LocalTasks) -> None:
        """Bind the task registry and the running loop, and hook ``SIGTERM`` (chained to the previous handler).

        The deadline starts at the signal, not at the lifespan shutdown: uvicorn waits for open HTTP requests
        before it reaches the lifespan, and that wait must not delay the cancel at the bound.
        Handlers can only be set from the main thread; elsewhere only the lifespan shutdown drains.

        Args:
            registry: The process-local task registry.
        """
        self._registry = registry
        self._loop = asyncio.get_running_loop()
        try:
            self._previous = signal.signal(signal.SIGTERM, self._on_sigterm)
        except ValueError:
            log.warning('Task drain signal handler not installed: not in the main thread')

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        """Record the termination time, start draining, schedule quiesce, and chain the previous handler.

        Args:
            signum: Signal number received by the process.
            frame: Interrupted stack frame, if any.
        """
        if self._terminated_at is None:
            self._terminated_at = time.monotonic()
        self.begin_draining()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._start_quiesce)
        previous = self._previous
        if callable(previous):
            previous(signum, frame)
        else:
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    async def watch_drain_file(self) -> None:
        """Enter ``draining`` as soon as the configured drain file exists (a supervisor's early drain request)."""
        while not self._draining:
            if os.path.exists(self._settings.drain_file):
                log.warning('Drain file %s found: draining', self._settings.drain_file)
                self.begin_draining()
                return
            await asyncio.sleep(DRAIN_FILE_POLL_SECONDS)

    def admit(self, request: Request) -> None:
        """Refuse a new chat while draining, but only an external HTTP request.

        Server-started work (scheduler, timers, automations, subagent continuations) builds its own request without a
        matched route and must run to completion.

        Args:
            request: The chat completion request.

        Raises:
            HTTPException: 503 with ``Retry-After`` for an external request while draining.
        """
        external = 'endpoint' in request.scope and getattr(request.state, 'internal', False) is not True
        if self._draining and external:
            raise HTTPException(status_code=503, detail='Server is draining', headers={'Retry-After': '1'})

    def cancel_error(self) -> str | None:
        """Return the error text when the current task is being cancelled by the drain.

        Returns:
            The text to store on the message and emit, or ``None`` for any other cancellation.
        """
        current = asyncio.current_task()
        return DRAIN_ERROR_TEXT if current is not None and current in self._cancelled else None

    def _start_quiesce(self) -> None:
        """Schedule the quiesce coroutine once if the registry has been attached."""
        if self._quiesce_task is None and self._registry is not None:
            self._quiesce_task = asyncio.create_task(self._quiesce(self._registry))

    async def quiesce(self) -> None:
        """Wait until the drain has finished (starting it if the stop signal did not).

        Called from the lifespan shutdown. The wait, the cancel at the bound and the key cleanup have usually
        already run since the signal.
        """
        self.begin_draining()
        self._start_quiesce()
        if self._quiesce_task is not None:
            await self._quiesce_task

    async def _quiesce(self, registry: LocalTasks) -> None:
        """Wait for running tasks up to the drain bound, cancel any remainder, and clean up all seen keys.

        Args:
            registry: The process-local task registry.
        """
        deadline = (self._terminated_at or time.monotonic()) + self._settings.max_stream_seconds
        items: dict[str, str | None] = {}
        self._remember(registry, items)
        await self._wait_for_tasks(registry, deadline, items)
        if pending := self._pending(registry):
            await self._cancel(pending)
        self._remember(registry, items)
        for task_id, item_id in items.items():
            try:
                await registry.cleanup(task_id, item_id)
            except Exception:
                log.exception('Task key cleanup failed for %s', task_id)
        log.info('Drain finished: %d task(s) handled', len(items))

    async def _wait_for_tasks(self, registry: LocalTasks, deadline: float, items: dict[str, str | None]) -> None:
        """Wait until no task runs, the deadline passes, and the settle period is over.

        The settle period lets a run that was claimed just before draining reach its task registration.

        Args:
            registry: The process-local task registry.
            deadline: Monotonic timestamp after which remaining tasks are cancelled.
            items: Mutable map accumulating ``task_id -> item_id`` seen during the drain.
        """
        settled_at = (self._draining_since or time.monotonic()) + self._settings.drain_settle_seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if pending := self._pending(registry):
                await asyncio.wait(pending, timeout=remaining)
            elif (settle := settled_at - time.monotonic()) > 0:
                await asyncio.sleep(min(settle, remaining))
            else:
                return
            self._remember(registry, items)

    @staticmethod
    def _pending(registry: LocalTasks) -> set[asyncio.Task]:
        """Return all local tasks that have not yet finished.

        Args:
            registry: The process-local task registry.

        Returns:
            Set of unfinished asyncio tasks.
        """
        return {task for task in registry.local().values() if not task.done()}

    @staticmethod
    def _remember(registry: LocalTasks, items: dict[str, str | None]) -> dict[str, str | None]:
        """Record every currently registered local task and its item id in ``items``.

        Args:
            registry: The process-local task registry.
            items: Mutable map accumulating ``task_id -> item_id``.

        Returns:
            The updated ``items`` map.
        """
        for task_id in registry.local():
            items.setdefault(task_id, registry.item_of(task_id))
        return items

    async def _cancel(self, pending: set[asyncio.Task]) -> None:
        """Cancel tasks still running at the drain bound and wait up to the cancel grace period.

        Each cancelled task is tracked in ``self._cancelled`` until its done-callback removes it so
        ``cancel_error`` identifies drain cancellations without retaining completed tasks.

        Args:
            pending: Set of unfinished asyncio tasks to cancel.
        """
        log.warning('Drain bound reached: cancelling %d running task(s)', len(pending))
        for task in pending:
            if task.done():
                continue
            self._cancelled.add(task)
            task.add_done_callback(self._cancelled.discard)
            task.cancel()
        _, stuck = await asyncio.wait(pending, timeout=self._settings.cancel_grace_seconds)
        if stuck:
            log.error('%d cancelled task(s) did not finish within the grace period', len(stuck))
