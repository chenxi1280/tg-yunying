from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .payloads import GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE


CLAIM_WINDOW_SECONDS = 60
DEFAULT_DISPATCHER_SCOPE = "task_center_dispatch"
TARGET_ADMISSION_CLAIM_CLASS = "target_admission_retry"
SEARCH_MEMBERSHIP_CLAIM_CLASS = "search_join_membership"
SEARCH_SOURCE_CLAIM_CLASS = "search_join"
HARD_HOURLY_CLAIM_CLASS = "hard_hourly"
TARGET_ADMISSION_RETRY_TASK_TYPE = "target_admission_retry"
GROUP_BOT_ADMISSION_ACTION_TYPES = frozenset(
    {
        GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE,
        "group_bot_control_observation",
        "group_bot_confirmation_button",
    }
)
PRIORITY_CLAIM_CLASSES = (TARGET_ADMISSION_CLAIM_CLASS, SEARCH_MEMBERSHIP_CLAIM_CLASS)
SHARED_CAPACITY_ERROR = "shared_dispatch_capacity_insufficient"


@dataclass(frozen=True)
class DispatchClaimBinding:
    reservation_id: str
    window_id: str
    shard_allocation_id: str
    dispatcher_scope: str
    shard_total: int
    shard_index: int
    allocation_epoch: int
    claim_class: str
    reservation_reason: str
    urgency_score: int
    unserved_strict_classes: tuple[str, ...]


@dataclass(frozen=True)
class DispatchClaimPlan:
    candidate_action_ids: tuple[str, ...]
    bindings_by_action_id: Mapping[str, DispatchClaimBinding]


@dataclass(frozen=True)
class DispatchClaimDemand:
    tenant_id: int
    task_id: str
    claim_class: str
    shard_total: int
    shard_index: int
    action_ids: tuple[str, ...]
    required_claims: int
    urgency_score: int
    is_strict: bool

    @property
    def key(self) -> tuple[int, str, str, int, int]:
        return (self.tenant_id, self.task_id, self.claim_class, self.shard_total, self.shard_index)
