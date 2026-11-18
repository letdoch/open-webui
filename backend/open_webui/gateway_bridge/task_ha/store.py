"""The one place where the task HA package touches Open WebUI's task, chat and event code."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from redis.asyncio import Redis

from open_webui import tasks as task_module
from open_webui.gateway_bridge.task_ha.liveness import InstanceLiveness
from open_webui.gateway_bridge.task_ha.orphan_target import OrphanTarget
from open_webui.models.chats import Chats
from open_webui.socket.main import get_event_emitter
from open_webui.utils.chat import get_temporary_chat
from open_webui.utils.chat_id import is_saved_chat_id, is_temporary_chat_id

log = logging.getLogger(__name__)

ORPHAN_ERROR_TEXT = 'server_unavailable: the server handling this response stopped; please send it again.'
REAP_CLAIM_SECONDS = 60


class OpenWebuiTaskStore:
    """Task registry (``LocalTasks``) and orphan storage (``OrphanStore``) over ``open_webui.tasks``."""

    class _MessageMetaProbe(BaseModel):
        """Closed probe extracting ``task_id`` from an assistant message metadata map."""

        model_config = ConfigDict(extra='ignore')

        task_id: str | None = None

    class _MessageProbe(BaseModel):
        """Closed probe extracting ``meta`` and ``done`` from a stored chat message."""

        model_config = ConfigDict(extra='ignore')

        meta: OpenWebuiTaskStore._MessageMetaProbe | None = None
        done: bool | None = None

    class _ChatHistoryProbe(BaseModel):
        """Closed probe extracting ``currentId`` from a saved chat history payload."""

        model_config = ConfigDict(extra='ignore')

        current_id: str | None = Field(default=None, alias='currentId')

    class _ChatPayloadProbe(BaseModel):
        """Closed probe extracting ``history`` from a saved chat document."""

        model_config = ConfigDict(extra='ignore')

        history: OpenWebuiTaskStore._ChatHistoryProbe | None = None

    class _TempChatOwnerProbe(BaseModel):
        """Closed probe validating the owner user id of a temporary chat session."""

        model_config = ConfigDict(extra='ignore')

        user_id: str

    class _StreamSnapshotProbe(BaseModel):
        """Closed probe extracting ``message_id`` from a response stream snapshot."""

        model_config = ConfigDict(extra='ignore')

        message_id: str

    class _ErrorContentDict(TypedDict):
        """Error content payload nested inside message error upserts and events."""

        content: str

    class _MessageErrorDataDict(TypedDict):
        """Payload of a ``chat:message:error`` socket event."""

        error: OpenWebuiTaskStore._ErrorContentDict
        done: bool

    class _MessageErrorEventDict(TypedDict):
        """Event announcing that an orphaned chat message ended with an error."""

        type: str
        data: OpenWebuiTaskStore._MessageErrorDataDict

    class _UnsavedChatActiveDataDict(TypedDict):
        """Payload of a ``chat:active`` event for an unsaved chat."""

        active: bool

    class _SavedChatActiveDataDict(TypedDict):
        """Payload of a ``chat:active`` event for a saved chat including its folder id."""

        active: bool
        folder_id: str | None

    class _ChatActiveEventDict(TypedDict):
        """Event announcing that a chat has no remaining active tasks."""

        type: str
        data: OpenWebuiTaskStore._UnsavedChatActiveDataDict | OpenWebuiTaskStore._SavedChatActiveDataDict

    def __init__(self, redis: Redis | None, liveness: InstanceLiveness) -> None:
        """Bind the Valkey client and the instance identity.

        Args:
            redis: The shared Valkey client, ``None`` when Valkey is not configured.
            liveness: Identity and dead-owner check of this instance.
        """
        self._redis = redis
        self._liveness = liveness

    @staticmethod
    def _reap_key(task_id: str) -> str:
        """Return the Valkey reap-claim key for a task id.

        Args:
            task_id: Identifier of the task being claimed.

        Returns:
            Full Valkey key name for the reap claim.
        """
        return f'{task_module.REDIS_TASKS_KEY}:reap:{task_id}'

    def local(self) -> Mapping[str, asyncio.Task]:
        """Return the live ``task_id -> task`` mapping.

        Returns:
            Process-local dictionary of active tasks.
        """
        return task_module.tasks

    def item_of(self, task_id: str) -> str | None:
        """Return the chat (item) id a task is filed under.

        Args:
            task_id: Identifier of the local task.

        Returns:
            The item id the task is filed under, or ``None`` if unassociated.
        """
        for item_id, task_ids in task_module.item_tasks.items():
            if task_id in task_ids:
                return item_id
        return None

    async def cleanup(self, task_id: str, item_id: str | None) -> None:
        """Remove a task from the local registry and Valkey.

        Args:
            task_id: Identifier of the task to remove.
            item_id: Chat (item) id the task was filed under, if any.
        """
        await task_module.cleanup_task(self._redis, task_id, item_id)

    def _live_local_tasks(self) -> list[tuple[str, asyncio.Task, str | None]]:
        """Snapshot local tasks that are still running and not currently cancelling.

        Returns:
            List of ``(task_id, task, item_id)`` tuples for running local tasks.
        """
        return [
            (task_id, task, self.item_of(task_id))
            for task_id, task in list(task_module.tasks.items())
            if not task.done() and not task.cancelling()
        ]

    async def _reregister_pipeline(
        self,
        redis: Redis,
        live: list[tuple[str, asyncio.Task, str | None]],
    ) -> list[tuple[str, asyncio.Task]]:
        """Re-write missing Valkey entries for ``live`` tasks and clean up any that finished concurrently.

        Args:
            redis: The shared Valkey client.
            live: Snapshot of running local tasks as ``(task_id, task, item_id)`` tuples.

        Returns:
            List of ``(task_id, task)`` pairs whose Valkey task key had been missing and was restored.
        """
        pipe = redis.pipeline(transaction=False)
        for task_id, _, item_id in live:
            pipe.set(
                f'{task_module.REDIS_TASKS_KEY}:{task_id}',
                self._liveness.instance_id,
                ex=task_module.REDIS_TASK_TTL or None,
                nx=True,
            )
            pipe.hset(task_module.REDIS_TASKS_KEY, task_id, item_id or '')
            if item_id:
                pipe.sadd(f'{task_module.REDIS_ITEM_TASKS_KEY}:{item_id}', task_id)
        results = iter(await pipe.execute())
        restored: list[tuple[str, asyncio.Task]] = []
        for task_id, task, item_id in live:
            was_missing = bool(next(results))
            next(results)
            if item_id:
                next(results)
            if was_missing:
                restored.append((task_id, task))
            if task_id not in task_module.tasks or task.done():
                await task_module.redis_cleanup_task(redis, task_id, item_id)
        return restored

    async def reregister(self) -> None:
        """Write the Valkey entries of every running, not cancelling local task again, if they are missing.

        Runs every heartbeat: a registration lost to a Valkey failover, or cleared by a peer that saw this instance
        as dead, is restored. ``SET NX`` leaves existing keys (and their TTL, kept by the stock heartbeat) alone.
        A task whose key had to be restored although a peer already reaped it (its message was ended with an error)
        is cancelled: the user was told it ended.
        """
        redis = self._redis
        live = self._live_local_tasks()
        if redis is None or not live:
            return
        restored = await self._reregister_pipeline(redis, live)
        await self._cancel_reaped(redis, restored)

    async def _cancel_reaped(self, redis: Redis, restored: list[tuple[str, asyncio.Task]]) -> None:
        """Cancel any restored local task whose reap claim exists in Valkey.

        Args:
            redis: The shared Valkey client.
            restored: Pairs of ``(task_id, task)`` whose task key had to be recreated.
        """
        if not restored:
            return
        pipe = redis.pipeline(transaction=False)
        for task_id, _ in restored:
            pipe.exists(self._reap_key(task_id))
        for (task_id, task), reaped in zip(restored, await pipe.execute()):
            if reaped and not task.done():
                log.warning('Task %s was ended by a peer while this instance was stalled: cancelling it', task_id)
                task.cancel()

    async def list_tasks(self, redis: Redis, item_id: str | None) -> list[str]:
        """Return the active task ids: owner alive, key present.

        A key that expired is cleared as in the stock code. A key of a dead owner is only hidden here: the reaper
        finalizes the message and clears the entries.

        Args:
            redis: The shared Valkey client.
            item_id: Chat id to list, or ``None`` for all tasks.

        Returns:
            The active task ids.
        """
        task_ids = list(
            await redis.smembers(f'{task_module.REDIS_ITEM_TASKS_KEY}:{item_id}')
            if item_id is not None
            else await redis.hkeys(task_module.REDIS_TASKS_KEY)
        )
        if not task_ids:
            return task_ids
        owners = await self.owners_of(redis, task_ids)
        dead = await self._liveness.dead_owners(redis, owners)
        active: list[str] = []
        for task_id, owner in zip(task_ids, owners):
            if owner is None:
                task_item_id = (
                    item_id if item_id is not None else await redis.hget(task_module.REDIS_TASKS_KEY, task_id)
                )
                await task_module.redis_cleanup_task(redis, task_id, task_item_id or None)
            elif owner not in dead:
                active.append(task_id)
        return active

    async def all_task_items(self, redis: Redis) -> Mapping[str, str]:
        """Return every registered ``task_id -> item_id``.

        Args:
            redis: The shared Valkey client.

        Returns:
            Mapping from registered task id to its item id string.
        """
        return await redis.hgetall(task_module.REDIS_TASKS_KEY)

    async def owners_of(self, redis: Redis, task_ids: list[str]) -> list[str | None]:
        """Return the owner value of each task key (``None`` when the key is gone).

        Args:
            redis: The shared Valkey client.
            task_ids: Task ids to look up in order.

        Returns:
            List of owner tokens (or ``None`` for missing keys) aligned with ``task_ids``.
        """
        pipe = redis.pipeline(transaction=False)
        for task_id in task_ids:
            pipe.get(f'{task_module.REDIS_TASKS_KEY}:{task_id}')
        return await pipe.execute()

    async def claim(self, redis: Redis, task_id: str) -> bool:
        """Claim a task for reaping with ``SET NX EX``; only one instance wins within the claim period.

        Args:
            redis: The shared Valkey client.
            task_id: Identifier of the task to claim.

        Returns:
            ``True`` when this instance acquired the claim, otherwise ``False``.
        """
        return bool(await redis.set(self._reap_key(task_id), '1', nx=True, ex=REAP_CLAIM_SECONDS))

    async def finish_orphan(self, redis: Redis, task_id: str, item_id: str | None) -> None:
        """End a dead instance's task: error on its message, events to the owner's clients, keys cleared.

        Finalizing is best effort; the keys are cleared in any case so that the entry cannot linger.

        Args:
            redis: The shared Valkey client.
            task_id: The orphaned task.
            item_id: The chat the task belongs to, if any.
        """
        target: OrphanTarget | None = None
        try:
            target = await self._locate(redis, task_id, item_id)
            if target is not None:
                await self._end_message(target)
        except Exception:
            log.exception('Could not finalize the message of orphaned task %s', task_id)
        await task_module.redis_cleanup_task(redis, task_id, item_id)
        try:
            if target is not None and not await task_module.has_active_tasks(redis, target.chat_id):
                await self._emit(target, await self._build_inactive_event(target))
        except Exception:
            log.exception('Could not announce chat %s inactive', item_id)

    @classmethod
    async def _find_unfinished_message_id(cls, chat_id: str, task_id: str) -> str | None:
        """Find the unfinished assistant message in ``chat_id`` tagged with ``task_id``.

        Args:
            chat_id: Identifier of the saved chat.
            task_id: Identifier of the orphaned task.

        Returns:
            Matching message id if found unfinished, otherwise ``None``.
        """
        messages = await Chats.get_messages_map_by_chat_id(chat_id)
        if messages is None:
            return None
        for message_id, raw_message in messages.items():
            try:
                probe = cls._MessageProbe.model_validate(raw_message)
            except ValidationError:
                log.debug(
                    'Skipping malformed message %s in chat %s while locating task %s',
                    message_id,
                    chat_id,
                    task_id,
                )
                continue
            if probe.done is False and probe.meta is not None and probe.meta.task_id == task_id:
                return message_id
        return None

    async def _locate_saved(self, chat_id: str, task_id: str) -> OrphanTarget | None:
        """Locate the orphan target for a persisted chat.

        Args:
            chat_id: Identifier of the saved chat.
            task_id: Identifier of the orphaned task.

        Returns:
            The resolved ``OrphanTarget``, or ``None`` if the chat or anchor message cannot be found.
        """
        chat = await Chats.get_chat_by_id(chat_id)
        if chat is None:
            return None
        matched_id = await self._find_unfinished_message_id(chat_id, task_id)
        if matched_id is not None:
            return OrphanTarget(
                user_id=chat.user_id, chat_id=chat_id, message_id=matched_id, saved=True, finalize=True
            )
        if chat.chat is None:
            return None
        try:
            payload = self._ChatPayloadProbe.model_validate(chat.chat)
        except ValidationError:
            return None
        current = payload.history.current_id if payload.history is not None else None
        if not current:
            return None
        return OrphanTarget(user_id=chat.user_id, chat_id=chat_id, message_id=current, saved=True, finalize=False)

    async def _locate_temporary(self, redis: Redis, task_id: str, chat_id: str) -> OrphanTarget | None:
        """Locate the orphan target for a temporary chat via its Valkey response stream snapshot.

        Args:
            redis: The shared Valkey client.
            task_id: Identifier of the orphaned task.
            chat_id: Identifier of the temporary chat.

        Returns:
            The resolved ``OrphanTarget``, or ``None`` if no snapshot or owner exists.
        """
        snapshot = await redis.get(f'{task_module.REDIS_RESPONSE_STREAMS_KEY}:{task_id}')
        owner = await get_temporary_chat(redis, chat_id)
        if not snapshot or not owner:
            return None
        try:
            owner_probe = self._TempChatOwnerProbe.model_validate(owner)
            stream_probe = self._StreamSnapshotProbe.model_validate(task_module.load_response_stream(snapshot))
        except ValidationError:
            return None
        return OrphanTarget(
            user_id=owner_probe.user_id,
            chat_id=chat_id,
            message_id=stream_probe.message_id,
            saved=False,
            finalize=True,
        )

    async def _locate(self, redis: Redis, task_id: str, chat_id: str | None) -> OrphanTarget | None:
        """Find the message of an orphan: by ``meta.task_id`` in a saved chat, by its stream snapshot otherwise.

        Args:
            redis: The shared Valkey client.
            task_id: Identifier of the orphaned task.
            chat_id: Chat id associated with the task, if any.

        Returns:
            The resolved ``OrphanTarget``, or ``None`` when the message cannot be addressed.
        """
        if not chat_id:
            return None
        if is_saved_chat_id(chat_id):
            return await self._locate_saved(chat_id, task_id)
        if is_temporary_chat_id(chat_id):
            return await self._locate_temporary(redis, task_id, chat_id)
        return None

    async def _end_message(self, target: OrphanTarget) -> None:
        """Persist the orphan error on a saved message and emit ``chat:message:error`` to the owner's room.

        Args:
            target: Resolved target describing the message to finalize.
        """
        if not target.finalize:
            return
        if target.saved:
            await Chats.upsert_message_to_chat_by_id_and_message_id(
                target.chat_id,
                target.message_id,
                {'error': {'content': ORPHAN_ERROR_TEXT}, 'done': True, 'meta': {'response_ended_at': time.time()}},
                touch=False,
            )
        error_event: OpenWebuiTaskStore._MessageErrorEventDict = {
            'type': 'chat:message:error',
            'data': {'error': {'content': ORPHAN_ERROR_TEXT}, 'done': True},
        }
        await self._emit(target, error_event)

    @staticmethod
    async def _build_inactive_event(target: OrphanTarget) -> OpenWebuiTaskStore._ChatActiveEventDict:
        """Build the ``chat:active=false`` socket event payload, including ``folder_id`` for saved chats.

        Args:
            target: Resolved target identifying the chat and owner.

        Returns:
            Typed ``chat:active`` event dictionary ready for emission.
        """
        if not target.saved:
            return {'type': 'chat:active', 'data': {'active': False}}
        folder_id = await Chats.get_chat_folder_id(target.chat_id, target.user_id)
        return {'type': 'chat:active', 'data': {'active': False, 'folder_id': folder_id}}

    @staticmethod
    async def _emit(
        target: OrphanTarget,
        event: OpenWebuiTaskStore._MessageErrorEventDict | OpenWebuiTaskStore._ChatActiveEventDict,
    ) -> None:
        """Emit a typed socket event anchored to ``target`` without mutating the database.

        Args:
            target: Resolved message anchor for routing the event to the owner's clients.
            event: Typed event payload to deliver to the emitter.
        """
        emitter = await get_event_emitter(
            {'user_id': target.user_id, 'chat_id': target.chat_id, 'message_id': target.message_id}, update_db=False
        )
        if emitter:
            await emitter(event)
