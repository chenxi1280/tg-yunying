"""Unified outbound target gate for Telegram send paths."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, OperationTarget, SchedulingSetting, Task, TgGroup
from app.services.task_center.group_send_limits import (
    GroupSendSlotBlock,
    active_window_block,
    group_policy_block,
)
from app.services.task_center.target_lifecycle import (
    LIFECYCLE_ACTIVE,
    resolve_action_target,
    terminal_target_block,
)
from app.services._common import audit


ERROR_TARGET_IDENTITY_UNRESOLVED = "target_identity_unresolved"
ERROR_TARGET_TENANT_MISMATCH = "target_tenant_mismatch"
GATE_MODE_DUAL_READ = "dual_read"
GATE_MODE_CANARY = "canary"
GATE_MODE_FULL = "full"
GATE_MODES = frozenset({GATE_MODE_DUAL_READ, GATE_MODE_CANARY, GATE_MODE_FULL})
CHANNEL_MESSAGE_ACTION_TYPES = frozenset(
    {"view_message", "like_message", "post_comment", "ensure_channel_membership", "ensure_target_membership"}
)


@dataclass(frozen=True)
class OutboundGateBlock:
    code: str
    detail: str
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class OutboundGateDiagnosticTarget:
    target_type: str
    target_id: str
    actor: str = "outbound-target-gate"


@dataclass(frozen=True)
class _OutboundGateContext:
    action: Action | None
    tenant_id: int | None
    mode: str
    diagnostic_target: OutboundGateDiagnosticTarget | None


def evaluate_target_lifecycle(
    target: OperationTarget | None,
    *,
    expected_revision: int | None = None,
    require_identity: bool = False,
) -> OutboundGateBlock | None:
    if target is None:
        if require_identity:
            return OutboundGateBlock(
                ERROR_TARGET_IDENTITY_UNRESOLVED,
                "无法解析稳定运营目标身份，已阻断出站",
            )
        return None
    if expected_revision is not None and int(expected_revision) != int(getattr(target, "reference_revision", 1) or 1):
        return OutboundGateBlock(
            "target_reference_superseded",
            "目标引用版本已变更，旧动作不得发往新引用",
        )
    terminal = terminal_target_block(target)
    if terminal is not None:
        return OutboundGateBlock(terminal.code, terminal.message)
    return None


def evaluate_outbound_target_gate(
    session: Session,
    *,
    action: Action | None = None,
    target: OperationTarget | None = None,
    group: TgGroup | None = None,
    outbound_peer: str | None = None,
    tenant_id: int | None = None,
    now: datetime | None = None,
    require_identity: bool = False,
    require_frozen_identity: bool = False,
    expected_target_id: int | None = None,
    expected_revision: int | None = None,
    expected_reference_snapshot: dict[str, str] | None = None,
    include_group_policy: bool = True,
    diagnostic_target: OutboundGateDiagnosticTarget | None = None,
) -> OutboundGateBlock | None:
    resolved_tenant_id = _resolved_tenant_id(action, target, group, tenant_id)
    context = _OutboundGateContext(
        action=action,
        tenant_id=resolved_tenant_id,
        mode=outbound_target_gate_mode(session, resolved_tenant_id),
        diagnostic_target=diagnostic_target,
    )
    block = _full_gate_identity_block(session, context)
    if block is not None:
        return block
    block = _target_gate_block(
        session,
        context,
        target=target,
        group=group,
        outbound_peer=outbound_peer,
        require_identity=require_identity,
        require_frozen_identity=require_frozen_identity,
        expected_target_id=expected_target_id,
        expected_revision=expected_revision,
        expected_reference_snapshot=expected_reference_snapshot,
    )
    if block is not None:
        return block
    return _group_policy_gate_block(session, action=action, group=group, now=now, include_group_policy=include_group_policy)


def _full_gate_identity_block(session: Session, context: _OutboundGateContext) -> OutboundGateBlock | None:
    if context.mode != GATE_MODE_FULL or context.action is None or _action_has_frozen_identity(session, context.action):
        return None
    return OutboundGateBlock(
        ERROR_TARGET_IDENTITY_UNRESOLVED,
        "动作缺少已冻结的目标身份，已阻断出站",
    )


def _target_gate_block(
    session: Session,
    context: _OutboundGateContext,
    *,
    target: OperationTarget | None,
    group: TgGroup | None,
    outbound_peer: str | None,
    require_identity: bool,
    require_frozen_identity: bool,
    expected_target_id: int | None,
    expected_revision: int | None,
    expected_reference_snapshot: dict[str, str] | None,
) -> OutboundGateBlock | None:
    resolved = resolve_outbound_target(session, action=context.action, target=target, group=group, outbound_peer=outbound_peer, tenant_id=context.tenant_id)
    block = _enforce_gate_block(session, context, _tenant_isolation_block(resolved, context.tenant_id), identity_bound=True)
    if block is not None:
        return block
    block = _frozen_identity_gate_block(
        session,
        context,
        target=resolved,
        expected_target_id=expected_target_id,
        expected_revision=expected_revision,
        expected_reference_snapshot=expected_reference_snapshot,
        required=require_frozen_identity,
    )
    if block is not None:
        return block
    identity_bound = _identity_bound(session, action=context.action, target=target, resolved=resolved, expected_reference_snapshot=expected_reference_snapshot)
    lifecycle = _lifecycle_gate_block(resolved, action=context.action, mode=context.mode, require_identity=require_identity, identity_bound=identity_bound, expected_revision=expected_revision)
    block = _enforce_gate_block(session, context, lifecycle, identity_bound=identity_bound)
    if block is not None:
        return block
    snapshot = _reference_snapshot_block(context.action, resolved, group, outbound_peer, expected_reference_snapshot)
    return _enforce_gate_block(session, context, snapshot, identity_bound=identity_bound)


def _frozen_identity_gate_block(
    session: Session,
    context: _OutboundGateContext,
    *,
    target: OperationTarget | None,
    expected_target_id: int | None,
    expected_revision: int | None,
    expected_reference_snapshot: dict[str, str] | None,
    required: bool,
) -> OutboundGateBlock | None:
    if not required:
        return None
    target_id, revision, snapshot = _frozen_identity_values(
        context.action,
        expected_target_id,
        expected_revision,
        expected_reference_snapshot,
    )
    block = frozen_target_identity_block(
        target,
        expected_target_id=target_id,
        expected_revision=revision,
        expected_reference_snapshot=snapshot,
    )
    if block is None:
        return None
    incomplete = _frozen_identity_incomplete(target_id, revision, snapshot)
    return _enforce_gate_block(session, context, block, identity_bound=not incomplete)


def _frozen_identity_values(
    action: Action | None,
    expected_target_id: int | None,
    expected_revision: int | None,
    expected_reference_snapshot: dict[str, str] | None,
) -> tuple[object, object, object]:
    if action is None:
        return expected_target_id, expected_revision, expected_reference_snapshot
    payload = action.payload if isinstance(action.payload, dict) else {}
    return (
        _action_frozen_target_id(action, payload),
        payload.get("target_reference_revision"),
        payload.get("target_reference_snapshot"),
    )


def frozen_target_identity_block(
    target: OperationTarget | None,
    *,
    expected_target_id: object,
    expected_revision: object,
    expected_reference_snapshot: object,
) -> OutboundGateBlock | None:
    if _frozen_identity_incomplete(expected_target_id, expected_revision, expected_reference_snapshot):
        return OutboundGateBlock(ERROR_TARGET_IDENTITY_UNRESOLVED, "动作缺少完整冻结目标身份，已阻断出站")
    if target is None:
        return OutboundGateBlock(ERROR_TARGET_IDENTITY_UNRESOLVED, "无法解析稳定运营目标身份，已阻断出站")
    if _positive_int(expected_target_id) != int(target.id):
        return _superseded_reference_block()
    if _positive_int(expected_revision) != int(target.reference_revision or 1):
        return _superseded_reference_block()
    snapshot_peer = str(expected_reference_snapshot["tg_peer_id"]).strip()
    if snapshot_peer != str(target.tg_peer_id or ""):
        return _superseded_reference_block()
    return None


def _frozen_identity_incomplete(target_id: object, revision: object, snapshot: object) -> bool:
    return (
        _positive_int(target_id) is None
        or _positive_int(revision) is None
        or not isinstance(snapshot, dict)
        or not str(snapshot.get("tg_peer_id") or "").strip()
    )


def _enforce_gate_block(
    session: Session,
    context: _OutboundGateContext,
    block: OutboundGateBlock | None,
    *,
    identity_bound: bool,
) -> OutboundGateBlock | None:
    return _enforce_lifecycle_block(
        context.action,
        block,
        session=session,
        tenant_id=context.tenant_id,
        mode=context.mode,
        identity_bound=identity_bound,
        diagnostic_target=context.diagnostic_target,
    )


def _identity_bound(
    session: Session,
    *,
    action: Action | None,
    target: OperationTarget | None,
    resolved: OperationTarget | None,
    expected_reference_snapshot: dict[str, str] | None,
) -> bool:
    return bool(
        resolved is not None
        or target is not None
        or _action_declares_target_identity(session, action)
        or _action_declares_reference_snapshot(action)
        or bool(expected_reference_snapshot and expected_reference_snapshot.get("tg_peer_id"))
    )


def _lifecycle_gate_block(
    resolved: OperationTarget | None,
    *,
    action: Action | None,
    mode: str,
    require_identity: bool,
    identity_bound: bool,
    expected_revision: int | None,
) -> OutboundGateBlock | None:
    revision, malformed_identity_block = _action_reference_revision(action, expected_revision)
    if malformed_identity_block is not None:
        return malformed_identity_block
    return evaluate_target_lifecycle(
        resolved,
        expected_revision=revision,
        require_identity=(mode == GATE_MODE_FULL) or require_identity or identity_bound,
    )


def _action_reference_revision(
    action: Action | None,
    expected_revision: int | None,
) -> tuple[int | None, OutboundGateBlock | None]:
    if expected_revision is not None:
        revision = _positive_int(expected_revision)
        if revision is not None:
            return revision, None
        return None, _malformed_reference_revision_block()
    if action is None:
        return None, None
    payload = action.payload if isinstance(action.payload, dict) else {}
    value = payload.get("target_reference_revision")
    if value is None:
        return None, None
    try:
        return int(value), None
    except (TypeError, ValueError, OverflowError):
        return None, _malformed_reference_revision_block()


def _malformed_reference_revision_block() -> OutboundGateBlock:
    return OutboundGateBlock(
        ERROR_TARGET_IDENTITY_UNRESOLVED,
        "动作目标引用版本格式无效，已阻断出站",
    )


def _group_policy_gate_block(
    session: Session,
    *,
    action: Action | None,
    group: TgGroup | None,
    now: datetime | None,
    include_group_policy: bool,
) -> OutboundGateBlock | None:
    if group is None or not include_group_policy:
        return None
    window = active_window_block(group, now)
    if window is not None:
        return _from_slot(window)
    if action is None:
        return None
    policy = group_policy_block(session, action=action, group=group)
    return _from_slot(policy) if policy is not None else None


def resolve_outbound_target(
    session: Session,
    *,
    action: Action | None = None,
    target: OperationTarget | None = None,
    group: TgGroup | None = None,
    outbound_peer: str | None = None,
    tenant_id: int | None = None,
) -> OperationTarget | None:
    if target is not None:
        return target
    if action is not None:
        target = resolve_action_target(session, action)
        if target is not None:
            return target
        if _action_declares_target_identity(session, action):
            return None
        payload = action.payload if isinstance(action.payload, dict) else {}
        peer_id = str(payload.get("chat_id") or "").strip()
        if peer_id:
            target = _target_for_peer(session, action.tenant_id, peer_id)
            if target is not None:
                return target
    if group is not None:
        return resolve_group_operation_target(session, group)
    peer_id = str(outbound_peer or "").strip()
    if peer_id and tenant_id is not None:
        return _target_for_peer(session, tenant_id, peer_id)
    return None


def resolve_group_operation_target(session: Session, group: TgGroup) -> OperationTarget | None:
    return _target_for_peer(session, group.tenant_id, group.tg_peer_id, target_type="group")


def group_lifecycle_allows_outbound(session: Session, group: TgGroup) -> OutboundGateBlock | None:
    return evaluate_outbound_target_gate(
        session,
        group=group,
        tenant_id=group.tenant_id,
        include_group_policy=False,
    )


def outbound_target_gate_mode(session: Session, tenant_id: int | None) -> str:
    setting = None
    if tenant_id is not None:
        setting = session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id == tenant_id))
    if setting is None:
        setting = session.scalar(select(SchedulingSetting).where(SchedulingSetting.tenant_id.is_(None)))
    mode = str(getattr(setting, "outbound_target_gate_mode", GATE_MODE_DUAL_READ) or GATE_MODE_DUAL_READ)
    if mode not in GATE_MODES:
        raise ValueError(f"未知出站目标门禁灰度状态: {mode}")
    return mode


def _target_for_peer(
    session: Session,
    tenant_id: int,
    peer_id: str,
    *,
    target_type: str | None = None,
) -> OperationTarget | None:
    conditions = [
        OperationTarget.tenant_id == tenant_id,
        OperationTarget.tg_peer_id == peer_id,
    ]
    if target_type is not None:
        conditions.append(OperationTarget.target_type == target_type)
    return session.scalar(select(OperationTarget).where(*conditions))


def _resolved_tenant_id(
    action: Action | None,
    target: OperationTarget | None,
    group: TgGroup | None,
    tenant_id: int | None,
) -> int | None:
    if action is not None:
        return int(action.tenant_id)
    if tenant_id is not None:
        return tenant_id
    if target is not None:
        return int(target.tenant_id)
    if group is not None:
        return int(group.tenant_id)
    return None


def _tenant_isolation_block(target: OperationTarget | None, tenant_id: int | None) -> OutboundGateBlock | None:
    if target is None or tenant_id is None or int(target.tenant_id) == int(tenant_id):
        return None
    return OutboundGateBlock(
        ERROR_TARGET_TENANT_MISMATCH,
        "目标不属于当前租户，已阻断出站",
    )


def _action_declares_target_identity(session: Session, action: Action | None) -> bool:
    if action is None:
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    if action.action_type in CHANNEL_MESSAGE_ACTION_TYPES:
        return bool(payload.get("channel_target_id"))
    if payload.get("target_operation_target_id") or payload.get("operation_target_id"):
        return True
    task = session.get(Task, action.task_id) if action.task_id else None
    config = task.type_config if task is not None and isinstance(task.type_config, dict) else {}
    return bool(config.get("target_operation_target_id"))


def _action_has_frozen_identity(session: Session, action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    snapshot = payload.get("target_reference_snapshot")
    target_id = _positive_int(_action_frozen_target_id(action, payload))
    if target_id is None or _frozen_identity_incomplete(target_id, payload.get("target_reference_revision"), snapshot):
        return False
    target = session.get(OperationTarget, target_id)
    return target is not None and int(target.tenant_id) == int(action.tenant_id)


def _action_frozen_target_id(action: Action, payload: dict) -> object:
    if action.action_type in CHANNEL_MESSAGE_ACTION_TYPES:
        return payload.get("channel_target_id")
    return payload.get("target_operation_target_id") or payload.get("operation_target_id")


def _action_declares_reference_snapshot(action: Action | None) -> bool:
    if action is None or not isinstance(action.payload, dict):
        return False
    snapshot = action.payload.get("target_reference_snapshot")
    return bool(isinstance(snapshot, dict) and snapshot.get("tg_peer_id"))


def _enforce_lifecycle_block(
    action: Action | None,
    block: OutboundGateBlock | None,
    *,
    session: Session,
    tenant_id: int | None,
    mode: str,
    identity_bound: bool,
    diagnostic_target: OutboundGateDiagnosticTarget | None,
) -> OutboundGateBlock | None:
    if block is None:
        return None
    if mode == GATE_MODE_DUAL_READ:
        _record_gate_diagnostic(session, action, tenant_id, mode, block, diagnostic_target)
        return None
    if mode == GATE_MODE_CANARY and block.code == ERROR_TARGET_IDENTITY_UNRESOLVED and not identity_bound:
        _record_gate_diagnostic(session, action, tenant_id, mode, block, diagnostic_target)
        return None
    return block


def _record_gate_diagnostic(
    session: Session,
    action: Action | None,
    tenant_id: int | None,
    mode: str,
    block: OutboundGateBlock,
    diagnostic_target: OutboundGateDiagnosticTarget | None,
) -> None:
    """Persist dual_read/canary diagnostics on the action and as durable audit.

    dual_read never blocks; without audit, would-block signals only live on
    mutable action.result and are hard to aggregate for canary readiness.
    """
    diagnostic = {
        "mode": mode,
        "code": block.code,
        "detail": block.detail,
    }
    if action is not None:
        action.result = {
            **(action.result or {}),
            "outbound_target_gate_diagnostic": diagnostic,
        }
    actor = "outbound-target-gate"
    target_type = "action"
    target_id = str(action.id) if action is not None else ""
    if diagnostic_target is not None:
        actor = diagnostic_target.actor
        target_type = diagnostic_target.target_type
        target_id = diagnostic_target.target_id
    if not target_id:
        return
    resolved_tenant_id = tenant_id if tenant_id is not None else (int(action.tenant_id) if action is not None else None)
    if resolved_tenant_id is None:
        return
    detail = f"mode={mode}; code={block.code}; detail={block.detail}"
    if action is not None and diagnostic_target is not None:
        detail = f"{detail}; action_id={action.id}"
    audit(
        session,
        tenant_id=resolved_tenant_id,
        actor=actor,
        action="出站目标门禁诊断",
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    )


def _reference_snapshot_block(
    action: Action | None,
    target: OperationTarget | None,
    group: TgGroup | None,
    outbound_peer: str | None,
    expected_reference_snapshot: dict[str, str] | None,
) -> OutboundGateBlock | None:
    payload = action.payload if action is not None and isinstance(action.payload, dict) else {}
    snapshot = expected_reference_snapshot or payload.get("target_reference_snapshot")
    peer_id = str(snapshot.get("tg_peer_id") or "").strip() if isinstance(snapshot, dict) else ""
    if not peer_id:
        return None
    if target is not None and peer_id != str(target.tg_peer_id or ""):
        return _superseded_reference_block()
    if group is not None and peer_id != str(group.tg_peer_id or ""):
        return _superseded_reference_block()
    if outbound_peer is not None and peer_id != str(outbound_peer or ""):
        return _superseded_reference_block()
    return None


def _superseded_reference_block() -> OutboundGateBlock:
    return OutboundGateBlock(
        "target_reference_superseded",
        "目标引用版本已变更，旧动作不得发往新引用",
    )


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed > 0 else None


def _from_slot(block: GroupSendSlotBlock) -> OutboundGateBlock:
    return OutboundGateBlock(block.failure_type, block.detail, block.retry_after_seconds)


__all__ = [
    "ERROR_TARGET_IDENTITY_UNRESOLVED",
    "ERROR_TARGET_TENANT_MISMATCH",
    "GATE_MODE_CANARY",
    "GATE_MODE_DUAL_READ",
    "GATE_MODE_FULL",
    "LIFECYCLE_ACTIVE",
    "OutboundGateDiagnosticTarget",
    "OutboundGateBlock",
    "evaluate_outbound_target_gate",
    "evaluate_target_lifecycle",
    "frozen_target_identity_block",
    "group_lifecycle_allows_outbound",
    "outbound_target_gate_mode",
    "resolve_outbound_target",
    "resolve_group_operation_target",
]
