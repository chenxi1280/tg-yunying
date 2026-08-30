from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    OperationTarget,
    RuleSet,
    RuleSetVersion,
    Task,
    TgAccount,
    TgAccountAuthorization,
    TgGroup,
    TgGroupAccount,
)
from app.models.enums import AccountStatus
from app.models.telegram_authorities import (
    TelegramGroupMutationAuthority,
    TelegramGroupMutationAuthorityHolder,
)
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.schemas.task_center import (
    GroupCloneConfig,
    GroupClonePrecheckResponse,
    GroupCloneTaskConfigUpdate,
    GroupCloneTaskCreate,
)
from app.services._common import _now, gateway
from app.services.developer_apps import credentials_for_authorization

from .group_mutation_authority import check_and_claim_exclusive_authority, compute_route_hash
from .group_clone_start_rows import initialize_start_rows
from .telegram_update_ingress import get_or_create_authorization_update_state

GROUP_CLONE_CONTRACT = "v2_group_clone"
OPEN_WRITER_STATUSES = ("pending", "claiming", "executing", "unknown_after_send")


@dataclass(frozen=True)
class CloneResolvedConfig:
    source_target: OperationTarget
    target_target: OperationTarget
    source_group: TgGroup
    target_group: TgGroup
    listener_authorization: TgAccountAuthorization
    control_authorization: TgAccountAuthorization
    sender_authorizations: tuple[TgAccountAuthorization, ...]
    listener_update_state: TelegramAuthorizationUpdateState


def request_fingerprint(payload: GroupCloneTaskCreate) -> str:
    data = payload.model_dump(mode="json", exclude={"client_request_id"})
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def precheck_group_clone(
    session: Session,
    tenant_id: int,
    payload: GroupCloneTaskCreate,
) -> GroupClonePrecheckResponse:
    blocks: list[str] = []
    warnings: list[str] = []
    resolved = _resolve_config(session, tenant_id, payload=payload, blocks=blocks)
    authority = _check_authority(session, tenant_id, payload=payload, blocks=blocks)
    _check_open_writers(session, tenant_id, payload=payload, blocks=blocks)
    _check_gateway_capabilities(blocks)
    if len(payload.sender_pool.account_ids) < 2:
        warnings.append("发送账号池少于 2 个账号，多个源发言人会进入 waiting_binding")
    return GroupClonePrecheckResponse(
        passed=not blocks,
        precheck_fingerprint=request_fingerprint(payload),
        authority_version=int(authority.version or 0) if authority else 0,
        hard_blocks=blocks,
        warnings=warnings,
        source_info=_source_info(payload, resolved),
        target_info=_target_info(payload, resolved),
        sender_pool_info={"total_accounts": len(payload.sender_pool.account_ids)},
    )


def create_group_clone_task(
    session: Session,
    tenant_id: int,
    user_id: int,
    *,
    payload: GroupCloneTaskCreate,
) -> tuple[Task, bool]:
    fingerprint = request_fingerprint(payload)
    existing = _idempotent_task(session, user_id, payload.client_request_id)
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise ValueError("client_request_id 已用于不同的 group_clone 配置")
        return existing, False
    precheck = precheck_group_clone(session, tenant_id, payload)
    if precheck.hard_blocks:
        raise ValueError(f"group_clone 配置不可创建: {precheck.hard_blocks}")
    task = _new_task(tenant_id, user_id, payload=payload, fingerprint=fingerprint)
    session.add(task)
    session.flush()
    return task, True


def create_and_start_group_clone_task(
    session: Session,
    tenant_id: int,
    user_id: int,
    *,
    payload: GroupCloneTaskCreate,
) -> tuple[Task, bool]:
    task, created = create_group_clone_task(session, tenant_id, user_id, payload=payload)
    if not created:
        return task, False
    resolved = _resolved_or_raise(session, tenant_id, payload)
    route_hash = compute_route_hash(
        payload.source.peer_type,
        payload.source.peer_id,
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
    )
    claimed, reason, authority = check_and_claim_exclusive_authority(
        session,
        tenant_id,
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
        writer_kind="group_clone",
        writer_id=task.id,
        route_hash=route_hash,
    )
    if not claimed or authority is None:
        raise ValueError(f"目标群独占写权限申请失败: {reason}")
    initialize_start_rows(session, task, payload=payload, resolved=resolved, route_hash=route_hash)
    task.status = "pending"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "starting"}
    return task, True


def create_cutover_group_clone_task(
    session: Session,
    tenant_id: int,
    user_id: int,
    *,
    payload: GroupCloneTaskCreate,
) -> tuple[Task, bool]:
    fingerprint = request_fingerprint(payload)
    existing = _idempotent_task(session, user_id, payload.client_request_id)
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise ValueError("client_request_id 已用于不同的 group_clone 配置")
        return existing, False
    resolved = _resolved_or_raise(session, tenant_id, payload)
    task = _new_task(tenant_id, user_id, payload=payload, fingerprint=fingerprint)
    session.add(task)
    session.flush()
    route_hash = compute_route_hash(
        payload.source.peer_type,
        payload.source.peer_id,
        target_peer_type=payload.target.peer_type,
        target_peer_id=payload.target.peer_id,
    )
    initialize_start_rows(
        session, task, payload=payload, resolved=resolved, route_hash=route_hash,
    )
    task.status = "pending"
    task.stats = {**dict(task.stats or {}), "clone_start_state": "starting"}
    return task, True


def update_group_clone_config(
    session: Session,
    tenant_id: int,
    task_id: str,
    *,
    payload: GroupCloneTaskConfigUpdate,
) -> Task:
    task = _tenant_task(session, tenant_id, task_id, for_update=True)
    if task.type != "group_clone":
        raise ValueError("非 group_clone 任务")
    current = dict(task.type_config or {})
    mutable = payload.model_dump(mode="json")
    current_senders = set((current.get("sender_pool") or {}).get("account_ids") or [])
    requested_senders = set((mutable.get("sender_pool") or {}).get("account_ids") or [])
    if requested_senders != current_senders:
        raise ValueError("sender_pool.account_ids 变更必须走受控账号槽交接")
    task.type_config = {**current, **mutable}
    task.config_revision = int(task.config_revision or 1) + 1
    return task


def tenant_clone_task(session: Session, tenant_id: int, task_id: str) -> Task:
    task = _tenant_task(session, tenant_id, task_id, for_update=False)
    if task.type != "group_clone":
        raise ValueError("非 group_clone 任务")
    return task


def _resolve_config(
    session: Session,
    tenant_id: int,
    *,
    payload: GroupCloneTaskCreate,
    blocks: list[str],
) -> CloneResolvedConfig | None:
    source_target = _target(session, tenant_id, payload.source.operation_target_id)
    target_target = _target(session, tenant_id, payload.target.operation_target_id)
    source_group = _group(session, tenant_id, payload.source.internal_group_id)
    target_group = _group(session, tenant_id, payload.target.internal_group_id)
    _check_canonical_peers(payload, source_target, target_target=target_target, source_group=source_group, target_group=target_group, blocks=blocks)
    listener = _authorization(session, tenant_id, account_id=payload.source.listener_account_id, authorization_id=payload.source.authorization_id)
    control = _authorization(session, tenant_id, account_id=payload.target.control_account_id, authorization_id=payload.target.control_authorization_id)
    senders = tuple(_current_authorization(session, tenant_id, account_id) for account_id in payload.sender_pool.account_ids)
    update_states = _ensure_update_states(
        session,
        tenant_id,
        authorizations=(listener, control, *senders),
    )
    _check_accounts_and_memberships(
        session,
        payload,
        tenant_id=tenant_id,
        source_group=source_group,
        target_group=target_group,
        listener=listener,
        control=control,
        senders=senders,
        update_states=update_states,
        blocks=blocks,
    )
    _check_authorization_credentials(session, (listener, control, *senders), blocks)
    _check_rule_set(session, tenant_id, payload=payload, blocks=blocks)
    update_state = update_states.get(listener.id) if listener else None
    if not _update_ingress_ready(update_state):
        blocks.append("listener authorization 的共享 Update Ingress 无有效 owner/lease")
    return _resolved_clone_config(
        source_target=source_target,
        target_target=target_target,
        source_group=source_group,
        target_group=target_group,
        listener_authorization=listener,
        control_authorization=control,
        sender_authorizations=senders,
        listener_update_state=update_state,
    )


def _resolved_clone_config(**values) -> CloneResolvedConfig | None:
    senders = values["sender_authorizations"]
    if any(value is None for key, value in values.items() if key != "sender_authorizations"):
        return None
    if any(item is None for item in senders):
        return None
    return CloneResolvedConfig(**values)


def _check_canonical_peers(payload, source_target, *, target_target, source_group, target_group, blocks) -> None:
    expected = (
        (source_target, payload.source.peer_id, "source OperationTarget"),
        (target_target, payload.target.peer_id, "target OperationTarget"),
        (source_group, payload.source.peer_id, "source TgGroup"),
        (target_group, payload.target.peer_id, "target TgGroup"),
    )
    for row, peer_id, label in expected:
        if row is None:
            blocks.append(f"{label} 不存在或不属于当前租户")
        elif str(row.tg_peer_id) != peer_id:
            blocks.append(f"{label} canonical peer 与配置不一致")
    if payload.source.peer_id == payload.target.peer_id:
        blocks.append("源群与目标群不能相同")


def _check_accounts_and_memberships(
    session,
    payload,
    *,
    tenant_id,
    source_group,
    target_group,
    listener,
    control,
    senders,
    update_states,
    blocks,
) -> None:
    if listener is None:
        blocks.append("listener account/authorization 不存在、非 current 或不属于当前租户")
    if control is None:
        blocks.append("target control account/authorization 不存在、非 current 或不属于当前租户")
    elif not _update_ingress_ready(update_states.get(control.id)):
        blocks.append("target control authorization 的共享 Update Ingress 无有效 owner/lease")
    _check_online_accounts(session, tenant_id, payload, blocks=blocks)
    if source_group and not _membership(session, source_group.id, payload.source.listener_account_id, listener=True):
        blocks.append("listener 没有可证明的源群读取/listener 关系")
    if target_group and control and not _control_membership(session, target_group.id, control.account_id):
        blocks.append("target control 没有可证明的目标群管理员关系")
    for account_id, authorization in zip(payload.sender_pool.account_ids, senders):
        if authorization is None:
            blocks.append(f"sender account {account_id} 没有有效 current authorization")
        elif target_group and not _membership(session, target_group.id, account_id, can_send=True):
            blocks.append(f"sender account {account_id} 没有目标群可发送事实")
        elif not _update_ingress_ready(update_states.get(authorization.id)):
            blocks.append(f"sender account {account_id} 的共享 Update Ingress 无有效 owner/lease")


def _ensure_update_states(session, tenant_id, *, authorizations) -> dict[int, TelegramAuthorizationUpdateState]:
    states: dict[int, TelegramAuthorizationUpdateState] = {}
    for authorization in authorizations:
        if authorization is None or authorization.id in states:
            continue
        states[authorization.id] = get_or_create_authorization_update_state(
            session,
            tenant_id,
            account_id=authorization.account_id,
            authorization_id=authorization.id,
            session_generation=authorization.slot_generation,
        )
    return states


def _check_online_accounts(session, tenant_id, payload, *, blocks) -> None:
    account_ids = {
        payload.source.listener_account_id,
        payload.target.control_account_id,
        *payload.sender_pool.account_ids,
    }
    accounts = {
        account.id: account
        for account in session.scalars(select(TgAccount).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.id.in_(account_ids),
        ))
    }
    for account_id in sorted(account_ids):
        account = accounts.get(account_id)
        if account is None or account.deleted_at is not None:
            blocks.append(f"account {account_id} 不存在或已删除")
        elif account.status != AccountStatus.ACTIVE.value:
            blocks.append(f"account {account_id} 未处于在线状态")


def _check_rule_set(session: Session, tenant_id: int, *, payload: GroupCloneTaskCreate, blocks: list[str]) -> None:
    rule = session.scalar(select(RuleSet).where(RuleSet.id == payload.content.rule_set_id, RuleSet.tenant_id == tenant_id))
    version = session.scalar(select(RuleSetVersion).where(
        RuleSetVersion.rule_set_id == payload.content.rule_set_id,
        RuleSetVersion.tenant_id == tenant_id,
        RuleSetVersion.version == payload.content.rule_set_version,
        RuleSetVersion.status == "published",
    ))
    if rule is None or version is None:
        blocks.append("content rule set/version 不存在、未发布或不属于当前租户")


def _check_authorization_credentials(session, authorizations, blocks) -> None:
    checked: set[int] = set()
    for authorization in authorizations:
        if authorization is None or authorization.id in checked:
            continue
        checked.add(authorization.id)
        if not authorization.session_ciphertext:
            blocks.append(f"authorization {authorization.id} 缺少 session")
            continue
        try:
            credentials_for_authorization(session, authorization)
        except ValueError as exc:
            blocks.append(f"authorization {authorization.id} 开发者应用不可用: {exc}")


def _check_authority(session: Session, tenant_id: int, *, payload: GroupCloneTaskCreate, blocks: list[str]):
    authority = session.scalar(select(TelegramGroupMutationAuthority).where(
        TelegramGroupMutationAuthority.tenant_id == tenant_id,
        TelegramGroupMutationAuthority.target_peer_type == payload.target.peer_type,
        TelegramGroupMutationAuthority.target_peer_id == payload.target.peer_id,
    ))
    if authority is None:
        return None
    holders = session.scalars(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == authority.id,
        TelegramGroupMutationAuthorityHolder.state == "active",
    )).all()
    if holders or authority.mode not in {"shared", "vacant"}:
        blocks.append("目标群存在活动写入 holder 或不可领取的 authority mode")
    return authority


def _check_open_writers(session: Session, tenant_id: int, *, payload: GroupCloneTaskCreate, blocks: list[str]) -> None:
    count = session.scalar(select(Action.id).where(
        Action.tenant_id == tenant_id,
        Action.payload["target_peer_type"].as_string() == payload.target.peer_type,
        Action.payload["target_peer_id"].as_string() == payload.target.peer_id,
        Action.status.in_(OPEN_WRITER_STATUSES),
    ).limit(1))
    if count:
        blocks.append("目标群存在 pending/executing/unknown 平台写入 Action")


def _check_gateway_capabilities(blocks: list[str]) -> None:
    required = (
        "send_raw_mtproto_message",
        "create_raw_mtproto_forum_topic",
        "fetch_raw_channel_boundary",
        "fetch_raw_group_admin_rights",
    )
    missing = [name for name in required if not callable(getattr(gateway, name, None))]
    if missing:
        blocks.append(f"raw MTProto Gateway capability 未就绪: {missing}")


def _new_task(tenant_id, user_id, *, payload, fingerprint) -> Task:
    config = GroupCloneConfig(
        source=payload.source,
        target=payload.target,
        sender_pool=payload.sender_pool,
        pacing=payload.pacing,
        content=payload.content,
        lifecycle=payload.lifecycle,
        retention=payload.retention,
    ).model_dump(mode="json")
    return Task(
        tenant_id=tenant_id,
        name=payload.name,
        type="group_clone",
        status="stopped",
        priority=payload.priority,
        timezone=payload.timezone,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        max_duration_hours=payload.max_duration_hours,
        account_config=payload.account_config.model_dump(mode="json"),
        pacing_config=payload.pacing_config.model_dump(mode="json"),
        failure_policy=payload.failure_policy.model_dump(mode="json"),
        type_config=config,
        fulfillment_contract_version=GROUP_CLONE_CONTRACT,
        created_by_user_id=user_id,
        create_task_type="group_clone",
        client_request_id=payload.client_request_id,
        request_fingerprint=fingerprint,
        idempotency_legacy_unproven=False,
        stats={"clone_start_state": "stopped"},
    )


def _resolved_or_raise(session, tenant_id, payload) -> CloneResolvedConfig:
    blocks: list[str] = []
    resolved = _resolve_config(session, tenant_id, payload=payload, blocks=blocks)
    if resolved is None or blocks:
        raise ValueError(f"group_clone start precheck 漂移: {blocks}")
    return resolved


def _idempotent_task(session, user_id, client_request_id) -> Task | None:
    if not client_request_id:
        return None
    return session.scalar(select(Task).where(
        Task.created_by_user_id == user_id,
        Task.create_task_type == "group_clone",
        Task.client_request_id == client_request_id,
    ).with_for_update())


def _tenant_task(session, tenant_id, task_id, *, for_update):
    stmt = select(Task).where(Task.id == task_id, Task.tenant_id == tenant_id, Task.deleted_at.is_(None))
    task = session.scalar(stmt.with_for_update() if for_update else stmt)
    if task is None:
        raise LookupError("任务不存在")
    return task


def _target(session, tenant_id, target_id):
    return session.scalar(select(OperationTarget).where(OperationTarget.id == target_id, OperationTarget.tenant_id == tenant_id))


def _group(session, tenant_id, group_id):
    return session.scalar(select(TgGroup).where(TgGroup.id == group_id, TgGroup.tenant_id == tenant_id))


def _authorization(session, tenant_id, *, account_id, authorization_id):
    return session.scalar(select(TgAccountAuthorization).join(
        TgAccount,
        TgAccount.id == TgAccountAuthorization.account_id,
    ).where(
        TgAccountAuthorization.id == authorization_id,
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.tenant_id == tenant_id,
        TgAccountAuthorization.is_current.is_(True),
        TgAccountAuthorization.status == "active",
        TgAccountAuthorization.telegram_user_id_digest.is_not(None),
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
    ))


def _current_authorization(session, tenant_id, account_id):
    return session.scalar(select(TgAccountAuthorization).join(
        TgAccount,
        TgAccount.id == TgAccountAuthorization.account_id,
    ).where(
        TgAccountAuthorization.account_id == account_id,
        TgAccountAuthorization.tenant_id == tenant_id,
        TgAccountAuthorization.is_current.is_(True),
        TgAccountAuthorization.status == "active",
        TgAccountAuthorization.telegram_user_id_digest.is_not(None),
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
    ))


def _update_state(session, tenant_id, authorization):
    if authorization is None:
        return None
    return session.scalar(select(TelegramAuthorizationUpdateState).where(
        TelegramAuthorizationUpdateState.tenant_id == tenant_id,
        TelegramAuthorizationUpdateState.authorization_id == authorization.id,
        TelegramAuthorizationUpdateState.session_generation == authorization.slot_generation,
    ))


def _update_ingress_ready(state) -> bool:
    return bool(
        state
        and state.state == "live"
        and state.owner_id
        and state.lease_expires_at
        and state.lease_expires_at > _now()
    )


def _membership(session, group_id, account_id, *, listener=False, can_send=False):
    clauses = [TgGroupAccount.group_id == group_id, TgGroupAccount.account_id == account_id]
    if listener:
        clauses.append(TgGroupAccount.is_listener.is_(True))
    if can_send:
        clauses.append(TgGroupAccount.can_send.is_(True))
    return session.scalar(select(TgGroupAccount).where(*clauses))


def _control_membership(session, group_id, account_id):
    link = _membership(session, group_id, account_id)
    return link if link and any(value in link.permission_label.lower() for value in ("admin", "管理员", "owner", "群主")) else None


def _source_info(payload, resolved):
    return {"peer_id": payload.source.peer_id, "resolved": resolved is not None, "listener_account_id": payload.source.listener_account_id}


def _target_info(payload, resolved):
    return {"peer_id": payload.target.peer_id, "resolved": resolved is not None, "control_account_id": payload.target.control_account_id}


__all__ = [
    "create_and_start_group_clone_task",
    "create_cutover_group_clone_task",
    "create_group_clone_task",
    "precheck_group_clone",
    "request_fingerprint",
    "tenant_clone_task",
    "update_group_clone_config",
]
