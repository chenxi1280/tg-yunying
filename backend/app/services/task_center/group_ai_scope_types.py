from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.models import (
    AiGroupMessageMemory,
    OperationTarget,
    Task,
    TgGroup,
)


@dataclass(frozen=True)
class GroupAiScopeViolation:
    field: str
    detail: str
    reason_code: str = "cross_group_content_scope_mismatch"

    @property
    def code(self) -> str:
        return self.reason_code


@dataclass(frozen=True)
class GroupAiScopeFacts:
    tasks: Mapping[str, Task]
    groups: Mapping[int, TgGroup]
    operation_targets: Mapping[int, OperationTarget]
    context_keys: frozenset[tuple[int, int, int]]
    reply_target_keys: frozenset[tuple[int, str, int, str]]
    memories: Mapping[str, AiGroupMessageMemory]
    account_link_ids: Mapping[tuple[int, int, int], int]


__all__ = ["GroupAiScopeFacts", "GroupAiScopeViolation"]
