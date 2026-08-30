from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TelegramNormalizedUpdate:
    identity_key: str
    constructor_name: str
    pts: int | None = None
    pts_count: int | None = None
    routing_peer_type: str | None = None
    routing_peer_id: str | None = None
    normalized_items: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TelegramOutboundMessageMapping:
    random_id: int
    remote_message_id: int
    update_identity_key: str


@dataclass(frozen=True)
class TelegramDifferenceBatch:
    scope: str
    status: str
    cursor: dict[str, int]
    updates: tuple[TelegramNormalizedUpdate, ...] = field(default_factory=tuple)
    outbound_mappings: tuple[TelegramOutboundMessageMapping, ...] = field(default_factory=tuple)
    final: bool = True


__all__ = [
    "TelegramDifferenceBatch",
    "TelegramNormalizedUpdate",
    "TelegramOutboundMessageMapping",
]
