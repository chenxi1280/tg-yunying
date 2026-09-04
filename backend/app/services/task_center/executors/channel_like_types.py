from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.models import ChannelMessage


@dataclass(frozen=True)
class LikePlanItem:
    message: ChannelMessage
    account_id: int
    reaction: str
    slot_ordinal: int
    plan_total: int
    obligation_id: str = ""


@dataclass(frozen=True)
class LikePlanningSpec:
    config: dict
    messages: list[ChannelMessage]
    accounts: list
    reactions: list[str]
    target_per_message: int
    account_ids_by_message: dict[int, set[int]]
    allocated_ids_by_message: dict[int, list[int]] | None
    now: datetime
