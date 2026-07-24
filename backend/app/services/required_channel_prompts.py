from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, OperationTarget, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now


OPEN_MEMBERSHIP_STATUSES = {"pending", "claiming", "executing", "retryable_failed"}
REQUIRED_CHANNEL_PERMISSION_LABEL = "已关注必需频道并可发言"
REQUIRED_CHANNEL_BLOCKED_LABEL = "待关注必需频道后复检"
REQUIRED_CHANNEL_PROMPT_PREVIEW_LENGTH = 500
REQUIRED_CHANNEL_PROMPT_SCHEDULE_STEP_SECONDS = 2
RUNNING_TASK_STATUSES = {"running", "执行中"}


def required_channel_references(detail: str) -> list[str]:
    refs: list[str] = []
    refs.extend(match.group("username") for match in re.finditer(r"@(?P<username>[A-Za-z0-9_]{4,})", detail or ""))
    refs.extend(match.group("username") for match in re.finditer(r"(?:https?://)?(?:t\.me|telegram\.me)/(?!joinchat/|\+)(?P<username>[A-Za-z0-9_]{4,})", detail or ""))
    refs.extend(_private_invite_ref(match.group(0)) for match in re.finditer(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)[A-Za-z0-9_-]{8,}", detail or ""))
    return _dedupe_refs(refs)


def required_channel_prompt_applies_to_send(text: str, account: TgAccount, *, allow_global: bool) -> bool:
    if not required_channel_prompt_is_group_rule(text):
        return False
    if required_channel_prompt_targets_account(text, account):
        return True
    return bool(allow_global and required_channel_prompt_is_global(text))


def required_channel_prompt_is_group_rule(text: str) -> bool:
    normalized = text.lower()
    has_reference = bool(required_channel_references(text))
    has_channel = "频道" in text or "channel" in normalized or has_reference
    has_follow = any(marker in normalized for marker in ("关注", "follow", "subscribe", "join"))
    has_speech = any(marker in normalized for marker in ("发言", "发送消息", "send", "speak", "write"))
    return has_channel and has_follow and has_speech


def required_channel_prompt_targets_account(text: str, account: TgAccount) -> bool:
    normalized = _normalized_prompt_text(text)
    return any(identifier in normalized for identifier in _account_identifiers(account))


def apply_required_channel_prompt_admission(session: Session, group: TgGroup, text: str, *, remote_message_id: str = "") -> int:
    if not required_channel_prompt_is_group_rule(text):
        return 0
    target = _operation_target_for_group(session, group)
    changed = 0
    for index, account in enumerate(_prompt_target_accounts(session, group, text)):
        link = _group_account_link(session, group, account)
        if not link:
            continue
        _mark_required_channel_pending(link, text)
        changed += _create_or_fast_track_memberships(session, group, target, account, text, remote_message_id, index)
    return changed


def _prompt_target_accounts(session: Session, group: TgGroup, text: str) -> list[TgAccount]:
    accounts = session.scalars(
        select(TgAccount)
        .join(TgGroupAccount, TgGroupAccount.account_id == TgAccount.id)
        .where(
            TgAccount.tenant_id == group.tenant_id,
            TgAccount.deleted_at.is_(None),
            TgGroupAccount.group_id == group.id,
        )
        .order_by(TgAccount.id.asc())
    )
    return [account for account in accounts if required_channel_prompt_targets_account(text, account)]


def _operation_target_for_group(session: Session, group: TgGroup) -> OperationTarget | None:
    return session.scalar(
        select(OperationTarget)
        .where(
            OperationTarget.tenant_id == group.tenant_id,
            OperationTarget.target_type == "group",
            OperationTarget.tg_peer_id == group.tg_peer_id,
        )
        .order_by(OperationTarget.id.asc())
        .limit(1)
    )


def _group_account_link(session: Session, group: TgGroup, account: TgAccount) -> TgGroupAccount | None:
    return session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == group.tenant_id,
            TgGroupAccount.group_id == group.id,
            TgGroupAccount.account_id == account.id,
        )
    )


def _mark_required_channel_pending(link: TgGroupAccount, text: str) -> None:
    link.can_send = False
    link.permission_label = f"{REQUIRED_CHANNEL_BLOCKED_LABEL}:{text[:40]}"[:80]


def _create_or_fast_track_memberships(
    session: Session,
    group: TgGroup,
    target: OperationTarget | None,
    account: TgAccount,
    text: str,
    remote_message_id: str,
    index: int,
) -> int:
    if target is None:
        return 0
    tasks = _running_group_ai_tasks_for_target(session, target, group)
    for task in tasks:
        _create_or_fast_track_membership_action(session, task, target, account, text, remote_message_id, index)
    return len(tasks)


def _running_group_ai_tasks_for_target(session: Session, target: OperationTarget, group: TgGroup) -> list[Task]:
    rows = session.scalars(
        select(Task)
        .where(
            Task.tenant_id == target.tenant_id,
            Task.type == "group_ai_chat",
            Task.status.in_(RUNNING_TASK_STATUSES),
            Task.deleted_at.is_(None),
        )
        .order_by(Task.created_at.asc())
    )
    return [task for task in rows if _task_targets_group(task, target, group)]


def _task_targets_group(task: Task, target: OperationTarget, group: TgGroup) -> bool:
    config = task.type_config or {}
    target_id = int(config.get("target_operation_target_id") or 0)
    group_id = int(config.get("target_group_id") or 0)
    return target_id == target.id or group_id == group.id


def _create_or_fast_track_membership_action(
    session: Session,
    task: Task,
    target: OperationTarget,
    account: TgAccount,
    text: str,
    remote_message_id: str,
    index: int,
) -> None:
    existing = _open_membership_action(session, task, target, account)
    scheduled_at = _now() + timedelta(seconds=REQUIRED_CHANNEL_PROMPT_SCHEDULE_STEP_SECONDS * index)
    result = _membership_prompt_result(text, remote_message_id)
    if existing:
        existing.scheduled_at = min(existing.scheduled_at.replace(tzinfo=None), scheduled_at) if existing.scheduled_at else scheduled_at
        existing.result = {**(existing.result or {}), **result}
        return
    action = _create_membership_action(session, task, account, scheduled_at, _membership_payload(target))
    action.result = result


def _open_membership_action(session: Session, task: Task, target: OperationTarget, account: TgAccount) -> Action | None:
    return session.scalar(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.account_id == account.id,
            Action.action_type.in_(["ensure_target_membership", "ensure_channel_membership"]),
            Action.status.in_(OPEN_MEMBERSHIP_STATUSES),
            Action.payload["channel_target_id"].as_integer() == target.id,
        )
        .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
        .limit(1)
    )


def _membership_payload(target: OperationTarget) -> dict[str, object]:
    return {
        "channel_id": target.tg_peer_id,
        "channel_target_id": target.id,
        "target_reference_revision": int(target.reference_revision or 1),
        "target_reference_snapshot": {
            "tg_peer_id": str(target.tg_peer_id),
            "username": str(target.username or ""),
            "title": str(target.title),
        },
        "target_type": "group",
        "target_display": target.title,
        "target_username": target.username or "",
        "invite_link": _joinable_target_reference(target),
        "require_send": True,
    }


def _create_membership_action(
    session: Session,
    task: Task,
    account: TgAccount,
    scheduled_at,
    payload: dict[str, object],
) -> Action:
    plan_batch_key = f"{task.id}:required-channel:{scheduled_at.isoformat()}"
    dedupe_key = _membership_action_dedupe_key(task, plan_batch_key, account.id, payload)
    existing = session.scalar(select(Action).where(Action.tenant_id == task.tenant_id, Action.action_dedupe_key == dedupe_key))
    if existing:
        return existing
    action = Action(
        id=str(uuid4()),
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type="ensure_target_membership",
        account_id=account.id,
        scheduled_at=scheduled_at,
        plan_batch_key=plan_batch_key,
        action_dedupe_key=dedupe_key,
        status="pending",
        payload=payload,
        result={},
    )
    session.add(action)
    return action


def _membership_action_dedupe_key(task: Task, plan_batch_key: str, account_id: int, payload: dict[str, object]) -> str:
    business_parts = {"action_type": "ensure_target_membership", "account_id": account_id, "payload": payload}
    raw = json.dumps(business_parts, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return f"{task.tenant_id}:{plan_batch_key}:{hashlib.sha256(raw).hexdigest()}"


def _membership_prompt_result(text: str, remote_message_id: str) -> dict[str, object]:
    result: dict[str, object] = {
        "reactivated_reason": "required_channel_prompt_detected",
        "validation_stage": "required_channel_follow",
        "required_channel_prompt": text[:REQUIRED_CHANNEL_PROMPT_PREVIEW_LENGTH],
    }
    refs = required_channel_references(text)
    if refs:
        result["required_channel_refs"] = refs
    if remote_message_id:
        result["required_channel_prompt_message_id"] = remote_message_id
    return result


def _joinable_target_reference(target: OperationTarget) -> str:
    if target.username:
        return f"https://t.me/{target.username.lstrip('@')}"
    value = str(target.tg_peer_id or "")
    return value if value.startswith(("https://t.me/", "http://t.me/", "t.me/", "+")) else ""


def _account_identifiers(account: TgAccount) -> list[str]:
    full_tg_name = " ".join(part for part in (account.tg_first_name, account.tg_last_name) if part)
    raw_values = [account.display_name, full_tg_name]
    if account.username:
        raw_values.append(f"@{account.username}")
    return [value for value in (_normalized_prompt_text(raw) for raw in raw_values) if value]


def required_channel_prompt_is_global(text: str) -> bool:
    compact = _normalized_prompt_text(text)
    return any(marker in compact for marker in ("您需要", "你需要", "才能发言", "before sending", "before you can"))


def _normalized_prompt_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _private_invite_ref(raw: str) -> str:
    value = raw.strip().strip("/.,，。；;)")
    return value if value.startswith("http") else f"https://{value}"


def _dedupe_refs(refs) -> list[str]:
    result: list[str] = []
    for raw in refs:
        ref = str(raw or "").strip().strip("/.,，。；;)")
        if ref and ref not in result:
            result.append(ref)
    return result
