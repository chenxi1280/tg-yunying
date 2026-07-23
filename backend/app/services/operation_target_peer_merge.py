from __future__ import annotations

import json

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiAccountGroupStanceMemory,
    AiGroupMessageMemory,
    Campaign,
    MessageFingerprint,
    OperationTarget,
    SearchJoinLinkedTaskDispatch,
    SearchJoinRankObservation,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)


FINGERPRINT_GROUP_SEGMENTS = ("relay", "target", "group_ai_chat")
ACTION_PAYLOAD_STREAM_BATCH_SIZE = 500
BLOCKING_ACTION_STATUSES = (
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "unknown_after_send",
    "waiting_cache",
    "failed",
)


def require_fresh_peer_merge_session(session: Session) -> None:
    if session.in_transaction():
        raise ValueError("stable Telegram peer canonicalization requires a fresh database session")


def begin_peer_merge_transaction(session: Session) -> None:
    require_fresh_peer_merge_session(session)
    session.connection(execution_options={"isolation_level": "SERIALIZABLE"})


def merge_duplicate_canonical_peer(
    session: Session,
    target: OperationTarget,
    group: TgGroup,
    snapshot,
    public_username: str,
) -> dict:
    target, group = _lock_canonical_rows(session, target, group)
    duplicate_target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.tenant_id == target.tenant_id,
            OperationTarget.tg_peer_id == snapshot.tg_peer_id,
            OperationTarget.id != target.id,
        ).with_for_update()
    )
    duplicate_group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == group.tenant_id,
            TgGroup.tg_peer_id == snapshot.tg_peer_id,
            TgGroup.id != group.id,
        ).with_for_update()
    )
    if not duplicate_target and not duplicate_group:
        return {}
    if not duplicate_target or not duplicate_group:
        raise ValueError("stable Telegram peer already assigned to another target or group; duplicate pair cannot be safely merged")
    _assert_duplicate_pair_is_mergeable(session, target, duplicate_target, group, duplicate_group, public_username)
    duplicate_ids = {"merged_duplicate_target_id": duplicate_target.id, "merged_duplicate_group_id": duplicate_group.id}
    _merge_duplicate_group_links(session, group, duplicate_group)
    session.flush()
    session.delete(duplicate_target)
    session.delete(duplicate_group)
    session.flush()
    return duplicate_ids


def _lock_canonical_rows(session: Session, target: OperationTarget, group: TgGroup) -> tuple[OperationTarget, TgGroup]:
    locked_tenant = session.scalar(select(Tenant).where(Tenant.id == target.tenant_id).with_for_update())
    locked_target = session.scalar(
        select(OperationTarget)
        .where(OperationTarget.id == target.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_group = session.scalar(
        select(TgGroup)
        .where(TgGroup.id == group.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not locked_tenant
        or not locked_target
        or not locked_group
        or locked_target.tenant_id != locked_tenant.id
        or locked_group.tenant_id != locked_target.tenant_id
    ):
        raise ValueError("target changed before duplicate peer merge")
    return locked_target, locked_group


def _assert_duplicate_pair_is_mergeable(
    session: Session,
    target: OperationTarget,
    duplicate_target: OperationTarget,
    group: TgGroup,
    duplicate_group: TgGroup,
    public_username: str,
) -> None:
    duplicate_username = str(duplicate_target.username or "").strip().lstrip("@").lower()
    if duplicate_target.target_type != target.target_type or duplicate_username != public_username:
        raise ValueError("stable Telegram peer already assigned to another target or group; duplicate pair cannot be safely merged")
    if _has_foreign_references(session, "operation_targets", duplicate_target.id):
        raise ValueError("stable Telegram peer already assigned to another target or group; duplicate target has business references")
    if _has_foreign_references(session, "tg_groups", duplicate_group.id, {"tg_group_accounts"}):
        raise ValueError("stable Telegram peer already assigned to another target or group; duplicate group has business references")
    if _has_non_foreign_group_reference(session, target.tenant_id, duplicate_group.id):
        raise ValueError("stable Telegram peer already assigned to another target or group; duplicate group has business references")
    if _has_runtime_config_reference(session, target.tenant_id, duplicate_target.id, duplicate_group.id):
        raise ValueError("stable Telegram peer already assigned to another target or group; duplicate pair is used by a task")


def _has_foreign_references(session: Session, table_name: str, identifier: int, allowed_tables: set[str] | None = None) -> bool:
    referenced = Base.metadata.tables[table_name]
    for table in Base.metadata.tables.values():
        if table.name in (allowed_tables or set()):
            continue
        for foreign_key in table.foreign_keys:
            if foreign_key.column.table is not referenced:
                continue
            count = session.scalar(select(func.count()).select_from(table).where(foreign_key.parent == identifier))
            if count:
                return True
    return False


def _has_non_foreign_group_reference(session: Session, tenant_id: int, group_id: int) -> bool:
    rows = (
        (AiGroupMessageMemory, AiGroupMessageMemory.group_id),
        (AiAccountGroupStanceMemory, AiAccountGroupStanceMemory.group_id),
        (SearchJoinLinkedTaskDispatch, SearchJoinLinkedTaskDispatch.target_group_id),
        (SearchJoinRankObservation, SearchJoinRankObservation.target_group_id),
    )
    for model, column in rows:
        if session.scalar(select(model.id).where(model.tenant_id == tenant_id, column == group_id)):
            return True
    if _has_message_fingerprint_reference(session, tenant_id, group_id):
        return True
    campaigns = session.execute(
        select(Campaign.target_group_ids, Campaign.source_group_ids, Campaign.selected_account_ids_by_group)
        .where(Campaign.tenant_id == tenant_id)
    )
    return any(_campaign_references_group(row, group_id) for row in campaigns)


def _has_message_fingerprint_reference(session: Session, tenant_id: int, group_id: int) -> bool:
    group_text = str(group_id)
    source_group_id = MessageFingerprint.source_group_id
    filters = [source_group_id == group_text]
    for segment in FINGERPRINT_GROUP_SEGMENTS:
        filters.extend((
            source_group_id.like(f"{segment}:{group_text}"),
            source_group_id.like(f"{segment}:{group_text}:%"),
            source_group_id.like(f"%:{segment}:{group_text}"),
            source_group_id.like(f"%:{segment}:{group_text}:%"),
        ))
    statement = select(MessageFingerprint.id).where(
        MessageFingerprint.tenant_id == tenant_id,
        or_(*filters),
    )
    return bool(session.scalar(statement))


def _csv_contains_id(value: str, expected_id: int) -> bool:
    return str(expected_id) in {item.strip() for item in str(value or "").split(",")}


def _campaign_references_group(row: tuple[str, str, str], group_id: int) -> bool:
    target_group_ids, source_group_ids, selected_accounts = row
    return (
        _csv_contains_id(target_group_ids, group_id)
        or _csv_contains_id(source_group_ids, group_id)
        or _selected_accounts_reference_group(selected_accounts, group_id)
    )


def _selected_accounts_reference_group(value: str, group_id: int) -> bool:
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        configured = json.loads(raw)
    except json.JSONDecodeError:
        return True
    return not isinstance(configured, dict) or str(group_id) in configured


def _has_runtime_config_reference(session: Session, tenant_id: int, target_id: int, group_id: int) -> bool:
    identifiers = {target_id, group_id}
    task_configs = session.execute(
        select(Task.type_config, Task.account_config, Task.pacing_config, Task.failure_policy)
        .where(Task.tenant_id == tenant_id)
    )
    if any(_config_references_ids(config, identifiers) for row in task_configs for config in row):
        return True
    payloads = session.scalars(
        select(Action.payload)
        .where(
            Action.tenant_id == tenant_id,
            Action.status.in_(BLOCKING_ACTION_STATUSES),
        )
        .execution_options(yield_per=ACTION_PAYLOAD_STREAM_BATCH_SIZE)
    )
    return any(_config_references_ids(payload, identifiers) for payload in payloads)


def _config_references_ids(value: object, identifiers: set[int], references_target_or_group: bool = False) -> bool:
    if isinstance(value, dict):
        return any(
            _config_references_ids(item, identifiers, references_target_or_group or _key_is_target_or_group(key))
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_config_references_ids(item, identifiers, references_target_or_group) for item in value)
    return references_target_or_group and _value_references_ids(value, identifiers)


def _value_references_ids(value: object, identifiers: set[int]) -> bool:
    raw_items = value.split(",") if isinstance(value, str) else [value]
    identifier_texts = {str(item) for item in identifiers}
    return any(str(item).strip() in identifier_texts for item in raw_items)


def _key_is_target_or_group(key: object) -> bool:
    normalized = str(key).lower()
    return "target" in normalized or "group" in normalized


def _merge_duplicate_group_links(session: Session, group: TgGroup, duplicate_group: TgGroup) -> None:
    rows = list(session.execute(_duplicate_group_link_rows_statement(duplicate_group.id)))
    destinations = {
        link.account_id: link
        for link in session.scalars(select(TgGroupAccount).where(TgGroupAccount.group_id == group.id).with_for_update())
    }
    for source, account in rows:
        _assert_link_tenant_matches(group, source, account)
        destination = destinations.get(source.account_id)
        if destination:
            _assert_link_tenant_matches(group, destination, account)
    for source, _account in rows:
        destination = destinations.get(source.account_id)
        if destination:
            _merge_group_account_link(destination, source)
            session.delete(source)
        else:
            source.group = group


def _duplicate_group_link_rows_statement(duplicate_group_id: int):
    return (
        select(TgGroupAccount, TgAccount)
        .outerjoin(TgAccount, TgAccount.id == TgGroupAccount.account_id)
        .where(TgGroupAccount.group_id == duplicate_group_id)
        .with_for_update(of=TgGroupAccount)
    )


def _assert_link_tenant_matches(group: TgGroup, link: TgGroupAccount, account: TgAccount | None) -> None:
    if link.tenant_id != group.tenant_id or not account or account.tenant_id != group.tenant_id:
        raise ValueError("duplicate group has a cross-tenant account link")


def _merge_group_account_link(destination: TgGroupAccount, source: TgGroupAccount) -> None:
    if source.can_send and not destination.can_send:
        destination.permission_label = source.permission_label
    destination.can_send = destination.can_send or source.can_send
    destination.is_listener = destination.is_listener or source.is_listener
    if source.last_sent_at and (
        destination.last_sent_at is None or source.last_sent_at > destination.last_sent_at
    ):
        destination.last_sent_at = source.last_sent_at
