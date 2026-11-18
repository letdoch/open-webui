"""Immutable target descriptor for an orphaned chat message."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OrphanTarget(BaseModel):
    """The chat message an orphaned task was producing, as far as it can be found."""

    model_config = ConfigDict(frozen=True, extra='forbid')

    user_id: str
    chat_id: str
    message_id: str
    """The message to finalize and the anchor of the events (the frontend ignores events of unknown messages)."""
    saved: bool
    """Whether the chat is stored: the message is finalized in the database and the folder id is announced."""
    finalize: bool
    """Whether the message was found unfinished and must get the error; otherwise only the chat's active state ends."""
