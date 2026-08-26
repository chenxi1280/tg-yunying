from __future__ import annotations

import json
from dataclasses import dataclass


PROFILE_ACTIONS = ("update_profile", "update_avatar")


@dataclass(frozen=True)
class ProfileTarget:
    name: str
    avatar_source: str
    avatar_object_key: str


@dataclass(frozen=True)
class ProfileGapReadback:
    actions: tuple[str, ...]
    target: ProfileTarget
    profile: object
    avatar: object
    local_fingerprint: object


def target_from_owner(owner) -> ProfileTarget | None:
    target = ProfileTarget(
        owner.profile_target_name,
        owner.profile_target_avatar_source,
        owner.profile_target_avatar_object_key,
    )
    return target if target.name and target.avatar_object_key else None


def requested_actions(raw_actions: str) -> tuple[str, ...]:
    values = tuple(json.loads(raw_actions or "[]"))
    return values or PROFILE_ACTIONS


def gap_actions(*, name_matches: bool, avatar_matches: bool) -> tuple[str, ...]:
    actions = []
    if not name_matches:
        actions.append("update_profile")
    if not avatar_matches:
        actions.append("update_avatar")
    return tuple(actions)


def freeze_created_target(owner, item, actions) -> None:
    if "update_profile" in actions:
        owner.profile_target_name = item.generated_display_name
    if "update_avatar" in actions:
        owner.profile_target_avatar_source = item.avatar_source


def freeze_completed_target(owner, account, item) -> None:
    owner.profile_item_id = item.id
    owner.profile_target_name = owner.profile_target_name or item.generated_display_name
    owner.profile_target_avatar_source = owner.profile_target_avatar_source or item.avatar_source
    owner.profile_target_avatar_object_key = account.avatar_object_key or ""


__all__ = [
    "ProfileGapReadback",
    "freeze_completed_target",
    "freeze_created_target",
    "gap_actions",
    "requested_actions",
    "target_from_owner",
]
