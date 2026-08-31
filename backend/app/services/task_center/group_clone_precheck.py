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
    TgAccount,
    TgAccountAuthorization,
    TgGroup,
    TgGroupAccount,
    Task,
)
from app.models.enums import AccountStatus
from app.models.telegram_authorities import (
    TelegramGroupMutationAuthority,
    TelegramGroupMutationAuthorityHolder,
)
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.schemas.task_center import GroupClonePrecheckResponse, GroupCloneTaskCreate
from app.services._common import _now, gateway
from app.services.developer_apps import credentials_for_authorization

from .telegram_update_ingress import get_or_create_authorization_update_state

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
    resolved = resolve_clone_config(session, tenant_id, payload=payload, blocks=blocks)
    _check_route_cycle(session, tenant_id, payload=payload, blocks=blocks)
    _check_fresh_control_rights(session, payload, resolved=resolved, blocks=blocks)
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


def resolve_clone_config(
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
    _check_canonical_peers(
        payload, source_target, target_target=target_target,
        source_group=source_group, target_group=target_group, blocks=blocks,
    )
    listener = _authorization(
        session, tenant_id, account_id=payload.source.listener_account_id,
        authorization_id=payload.source.authorization_id,
    )
    control = _authorization(
        session, tenant_id, account_id=payload.target.control_account_id,
        authorization_id=payload.target.control_authorization_id,
    )
    senders = tuple(
        _current_authorization(session, tenant_id, account_id)
        for account_id in payload.sender_pool.account_ids
    )
    states = _ensure_update_states(
        session, tenant_id, authorizations=(listener, control, *senders),
    )
    _check_accounts_and_memberships(
        session, payload, tenant_id=tenant_id,
        source_group=source_group, target_group=target_group,
        listener=listener, control=control, senders=senders,
        update_states=states, blocks=blocks,
    )
    _check_authorization_credentials(session, (listener, control, *senders), blocks)
    _check_rule_set(session, tenant_id, payload=payload, blocks=blocks)
    listener_state = states.get(listener.id) if listener else None
    if not _update_ingress_ready(listener_state):
        blocks.append("listener authorization 的共享 Update Ingress 无有效 owner/lease")
    return _resolved_clone_config(
        source_target=source_target, target_target=target_target,
        source_group=source_group, target_group=target_group,
        listener_authorization=listener, control_authorization=control,
        sender_authorizations=senders, listener_update_state=listener_state,
    )


def _resolved_clone_config(**values) -> CloneResolvedConfig | None:
    senders = values["sender_authorizations"]
    if any(value is None for key, value in values.items() if key != "sender_authorizations"):
        return None
    if any(item is None for item in senders):
        return None
    return CloneResolvedConfig(**values)


def _check_canonical_peers(
    payload, source_target, *, target_target, source_group, target_group, blocks,
) -> None:
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
    session, payload, *, tenant_id, source_group, target_group,
    listener, control, senders, update_states, blocks,
) -> None:
    if listener is None:
        blocks.append("listener account/authorization 不存在、非 current 或不属于当前租户")
    if control is None:
        blocks.append("target control account/authorization 不存在、非 current 或不属于当前租户")
    elif not _update_ingress_ready(update_states.get(control.id)):
        blocks.append("target control authorization 的共享 Update Ingress 无有效 owner/lease")
    _check_online_accounts(session, tenant_id, payload, blocks=blocks)
    if source_group and not _membership(
        session, source_group.id, payload.source.listener_account_id, listener=True,
    ):
        blocks.append("listener 没有可证明的源群读取/listener 关系")
    if target_group and control and not _control_membership(
        session, target_group.id, control.account_id,
    ):
        blocks.append("target control 没有可证明的目标群管理员关系")
    _check_sender_memberships(
        session, payload, target_group=target_group,
        senders=senders, update_states=update_states, blocks=blocks,
    )


def _check_sender_memberships(
    session, payload, *, target_group, senders, update_states, blocks,
) -> None:
    for account_id, authorization in zip(payload.sender_pool.account_ids, senders):
        if authorization is None:
            blocks.append(f"sender account {account_id} 没有有效 current authorization")
        elif target_group and not _membership(
            session, target_group.id, account_id, can_send=True,
        ):
            blocks.append(f"sender account {account_id} 没有目标群可发送事实")
        elif not _update_ingress_ready(update_states.get(authorization.id)):
            blocks.append(f"sender account {account_id} 的共享 Update Ingress 无有效 owner/lease")


def _ensure_update_states(
    session, tenant_id, *, authorizations,
) -> dict[int, TelegramAuthorizationUpdateState]:
    states: dict[int, TelegramAuthorizationUpdateState] = {}
    for authorization in authorizations:
        if authorization is None or authorization.id in states:
            continue
        states[authorization.id] = get_or_create_authorization_update_state(
            session, tenant_id, account_id=authorization.account_id,
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


def _check_rule_set(
    session: Session, tenant_id: int, *, payload: GroupCloneTaskCreate, blocks: list[str],
) -> None:
    _check_rule_binding(
        session, tenant_id, content=payload.content, blocks=blocks,
    )


def validate_clone_rule_binding(session: Session, tenant_id: int, content) -> None:
    blocks: list[str] = []
    _check_rule_binding(session, tenant_id, content=content, blocks=blocks)
    if blocks:
        raise ValueError(blocks[0])


def _check_rule_binding(session, tenant_id, *, content, blocks) -> None:
    rule = session.scalar(select(RuleSet).where(
        RuleSet.id == content.rule_set_id,
        RuleSet.tenant_id == tenant_id,
    ))
    version = session.scalar(select(RuleSetVersion).where(
        RuleSetVersion.rule_set_id == content.rule_set_id,
        RuleSetVersion.tenant_id == tenant_id,
        RuleSetVersion.version == content.rule_set_version,
        RuleSetVersion.status == "published",
    ))
    supported = rule is not None and "group_clone" in (rule.task_types or [])
    if not supported or version is None:
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


def _check_authority(
    session: Session, tenant_id: int, *, payload: GroupCloneTaskCreate, blocks: list[str],
):
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


def _check_open_writers(
    session: Session, tenant_id: int, *, payload: GroupCloneTaskCreate, blocks: list[str],
) -> None:
    writer = session.scalar(select(Action.id).where(
        Action.tenant_id == tenant_id,
        Action.payload["target_peer_type"].as_string() == payload.target.peer_type,
        Action.payload["target_peer_id"].as_string() == payload.target.peer_id,
        Action.status.in_(OPEN_WRITER_STATUSES),
    ).limit(1))
    if writer:
        blocks.append("目标群存在 pending/executing/unknown 平台写入 Action")


def _check_route_cycle(session, tenant_id, *, payload, blocks) -> None:
    rows = session.scalars(select(Task).where(
        Task.tenant_id == tenant_id,
        Task.type == "group_clone",
        Task.status.in_(("pending", "running", "paused")),
        Task.deleted_at.is_(None),
    )).all()
    edges = {_task_route_edge(task) for task in rows}
    edges.discard(None)
    source = (payload.source.peer_type, payload.source.peer_id)
    target = (payload.target.peer_type, payload.target.peer_id)
    if _would_create_route_cycle(edges, source=source, target=target):
        blocks.append("clone_route_cycle: 当前活动克隆路由会形成消息放大环路")


def _task_route_edge(task):
    config = task.type_config or {}
    source = config.get("source") or {}
    target = config.get("target") or {}
    if not source.get("peer_type") or not source.get("peer_id"):
        return None
    if not target.get("peer_type") or not target.get("peer_id"):
        return None
    return (
        (str(source["peer_type"]), str(source["peer_id"])),
        (str(target["peer_type"]), str(target["peer_id"])),
    )


def _would_create_route_cycle(edges, *, source, target) -> bool:
    graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for start, end in edges:
        graph.setdefault(start, set()).add(end)
    pending = [target]
    visited: set[tuple[str, str]] = set()
    while pending:
        node = pending.pop()
        if node == source:
            return True
        if node in visited:
            continue
        visited.add(node)
        pending.extend(graph.get(node, ()))
    return False


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


def _check_fresh_control_rights(session, payload, *, resolved, blocks) -> None:
    if resolved is None:
        return
    authorization = resolved.control_authorization
    try:
        rights = gateway.fetch_raw_group_admin_rights(
            payload.target.peer_id,
            session_ciphertext=authorization.session_ciphertext,
            credentials=credentials_for_authorization(session, authorization),
        )
    except Exception as exc:
        blocks.append(f"target control 实时权限读取失败: {exc}")
        return
    required = ("delete_messages", "pin_messages", "manage_topics")
    missing = [name for name in required if not bool(rights.get(name))]
    if missing:
        blocks.append(f"target control 实时管理员权限不足: {missing}")


def _target(session, tenant_id, target_id):
    return session.scalar(select(OperationTarget).where(
        OperationTarget.id == target_id,
        OperationTarget.tenant_id == tenant_id,
    ))


def _group(session, tenant_id, group_id):
    return session.scalar(select(TgGroup).where(
        TgGroup.id == group_id, TgGroup.tenant_id == tenant_id,
    ))


def _authorization(session, tenant_id, *, account_id, authorization_id):
    return session.scalar(select(TgAccountAuthorization).join(
        TgAccount, TgAccount.id == TgAccountAuthorization.account_id,
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
        TgAccount, TgAccount.id == TgAccountAuthorization.account_id,
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


def _update_ingress_ready(state) -> bool:
    return bool(
        state
        and state.state == "live"
        and state.owner_id
        and state.lease_expires_at
        and state.lease_expires_at > _now()
    )


def _membership(session, group_id, account_id, *, listener=False, can_send=False):
    clauses = [
        TgGroupAccount.group_id == group_id,
        TgGroupAccount.account_id == account_id,
    ]
    if listener:
        clauses.append(TgGroupAccount.is_listener.is_(True))
    if can_send:
        clauses.append(TgGroupAccount.can_send.is_(True))
    return session.scalar(select(TgGroupAccount).where(*clauses))


def _control_membership(session, group_id, account_id):
    link = _membership(session, group_id, account_id)
    labels = ("admin", "管理员", "owner", "群主")
    return link if link and any(value in link.permission_label.lower() for value in labels) else None


def _source_info(payload, resolved):
    return {
        "peer_id": payload.source.peer_id,
        "resolved": resolved is not None,
        "listener_account_id": payload.source.listener_account_id,
    }


def _target_info(payload, resolved):
    return {
        "peer_id": payload.target.peer_id,
        "resolved": resolved is not None,
        "control_account_id": payload.target.control_account_id,
    }


__all__ = [
    "CloneResolvedConfig",
    "precheck_group_clone",
    "request_fingerprint",
    "resolve_clone_config",
]
