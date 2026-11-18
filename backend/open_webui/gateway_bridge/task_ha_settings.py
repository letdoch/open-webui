"""Typed settings of the task drain and instance liveness feature."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

FAILOVER_MARGIN_SECONDS = 10


class TaskHaSettings(BaseModel):
    """Immutable, strictly validated settings of the ``task_ha`` block.

    Every field is inert while ``enabled`` is false, which keeps the stock behaviour.
    """

    model_config = ConfigDict(strict=True, frozen=True, extra='forbid')

    enabled: bool = False
    max_stream_seconds: int = Field(default=300, ge=1, le=86400)
    cancel_grace_seconds: int = Field(default=30, ge=1, le=300)
    instance_heartbeat_seconds: int = Field(default=5, ge=1, le=60)
    instance_ttl_seconds: int = Field(default=30, ge=1, le=600)
    valkey_failover_window_seconds: int = Field(default=15, ge=1, le=300)
    drain_file: str = ''
    drain_settle_seconds: int = Field(default=5, ge=0, le=60)

    @model_validator(mode='after')
    def _ttl_survives_failover(self) -> TaskHaSettings:
        """Require the instance TTL to outlast one Valkey failover plus a margin.

        After a failover the instance key must still be present on the promoted node when the next
        heartbeat arrives, otherwise every live instance would be declared dead at once.

        Returns:
            The validated settings.

        Raises:
            ValueError: When ``ttl - heartbeat`` is shorter than the failover window plus the margin.
        """
        needed = self.valkey_failover_window_seconds + FAILOVER_MARGIN_SECONDS
        if self.instance_ttl_seconds - self.instance_heartbeat_seconds < needed:
            raise ValueError(
                'instance_ttl_seconds - instance_heartbeat_seconds must be at least '
                f'valkey_failover_window_seconds + {FAILOVER_MARGIN_SECONDS} ({needed})'
            )
        return self
