from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, AuditLog, ExecutionAttempt, Task, TgAccountAuthorization
from app.models.group_clone import CloneSourceEvent, CloneSourceStreamState
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
)
from app.models.telegram_authorities import (
    TelegramGroupMutationAuthority,
    TelegramGroupMutationAuthorityHolder,
)

from .group_clone_lifecycle import create_cutover_group_clone_task
from .group_mutation_authority import compute_route_hash, ensure_legacy_shared_holder
from app.services._common import gateway
from app.services.developer_apps import credentials_for_authorization

OPEN_ACTION_STATES = ("pending", "claiming", "executing", "unknown_after_send")
UNSAFE_CUTOVER_STATES = ("claiming", "executing", "unknown_after_send")


def preview_clone_cutover(session: Session, legacy_task: Task, *, actor_id: int) -> dict:
    manifest = canonical_legacy_route(legacy_task)
    route_hash = compute_route_hash(
        manifest["source_peer_type"], manifest["source_peer_id"],
        target_peer_type=manifest["target_peer_type"],
        target_peer_id=manifest["target_peer_id"],
    )
    holder = ensure_legacy_shared_holder(
        session, legacy_task.tenant_id,
        target_peer_type=manifest["target_peer_type"],
        target_peer_id=manifest["target_peer_id"],
        writer_kind="group_relay", writer_id=legacy_task.id, route_hash=route_hash,
    )
    authority = session.get(TelegramGroupMutationAuthority, holder.authority_id)
    blockers = _cutover_blockers(session, legacy_task.id, authority, holder)
    if blockers:
        raise ValueError(f"cutover_blocked: {blockers}")
    action_fingerprint = _action_fingerprint(session, legacy_task.id)
    manifest_hash = _hash(manifest)
    token = _hash({
        "legacy_task_id": legacy_task.id,
        "legacy_revision": legacy_task.config_revision,
        "manifest_hash": manifest_hash,
        "authority_version": authority.version,
        "holder_version": holder.version,
        "open_action_fingerprint": action_fingerprint,
    })
    snapshot = {
        "preview_token": token,
        "route_manifest_hash": manifest_hash,
        "expected_authority_version": authority.version,
        "open_action_fingerprint": action_fingerprint,
    }
    legacy_task.stats = {**dict(legacy_task.stats or {}), "clone_cutover_preview": snapshot}
    session.add(_audit(legacy_task, actor_id, "cutover_previewed", snapshot))
    return {
        **snapshot,
        "legacy_task_id": legacy_task.id,
        "expected_legacy_revision": legacy_task.config_revision,
        "route_manifest": manifest,
    }


def apply_clone_cutover(session: Session, legacy_task: Task, *, request, actor_id: int) -> dict:
    replay = _request_replay(legacy_task, "clone_cutover_apply", request)
    if replay is not None:
        return replay
    preview = dict((legacy_task.stats or {}).get("clone_cutover_preview") or {})
    _validate_cutover_request(legacy_task, request, preview)
    manifest = canonical_legacy_route(legacy_task)
    _validate_clone_route(request.clone_config, manifest)
    authority, old_holder = _locked_cutover_authority(session, legacy_task, manifest)
    if authority.version != request.expected_authority_version:
        raise ValueError("cutover authority version 已变化")
    if _action_fingerprint(session, legacy_task.id) != request.open_action_fingerprint:
        raise ValueError("cutover open-action fingerprint 已变化")
    blockers = _cutover_blockers(session, legacy_task.id, authority, old_holder)
    if blockers:
        raise ValueError(f"cutover_blocked: {blockers}")
    clone, created = create_cutover_group_clone_task(
        session, legacy_task.tenant_id, actor_id, payload=request.clone_config,
    )
    if not created:
        raise ValueError("cutover clone task 已存在但未记录为本次割接")
    boundary = _freeze_clone_cutover_boundary(session, clone)
    _handoff_to_clone(session, authority, old_holder, clone, manifest)
    legacy_task.status = "paused"
    legacy_task.stats = {
        **dict(legacy_task.stats or {}),
        "clone_start_state": "cutover_paused",
        "cutover_clone_task_id": clone.id,
        "cutover_generation": authority.cutover_generation,
        "cutover_boundary": boundary,
    }
    clone.stats = {
        **dict(clone.stats or {}),
        "cutover_legacy_task_id": legacy_task.id,
        "cutover_generation": authority.cutover_generation,
    }
    result = {
        "success": True,
        "legacy_task_id": legacy_task.id,
        "clone_task_id": clone.id,
        "cutover_boundary": boundary,
    }
    _store_request(legacy_task, "clone_cutover_apply", request, result)
    session.add(_audit(legacy_task, actor_id, "cutover_applied", result))
    return result


def preview_clone_rollback(session: Session, clone_task: Task, *, actor_id: int) -> dict:
    legacy = _cutover_legacy_task(session, clone_task)
    authority = _clone_authority(session, clone_task, for_update=False)
    blockers = _rollback_blockers(session, clone_task.id)
    if blockers:
        raise ValueError(f"rollback_blocked: {blockers}")
    fingerprint = _action_fingerprint(session, clone_task.id)
    token = _hash({
        "clone_task_id": clone_task.id,
        "legacy_task_id": legacy.id,
        "authority_version": authority.version,
        "action_fingerprint": fingerprint,
    })
    snapshot = {
        "preview_token": token,
        "expected_authority_version": authority.version,
        "open_action_fingerprint": fingerprint,
        "legacy_task_id": legacy.id,
    }
    clone_task.stats = {**dict(clone_task.stats or {}), "clone_rollback_preview": snapshot}
    session.add(_audit(clone_task, actor_id, "rollback_previewed", snapshot))
    return {**snapshot, "clone_task_id": clone_task.id}


def apply_clone_rollback(session: Session, clone_task: Task, *, request, actor_id: int) -> dict:
    replay = _request_replay(clone_task, "clone_rollback_apply", request)
    if replay is not None:
        return replay
    preview = dict((clone_task.stats or {}).get("clone_rollback_preview") or {})
    if request.preview_token != preview.get("preview_token"):
        raise ValueError("rollback preview token 无效或已过期")
    if request.expected_authority_version != preview.get("expected_authority_version"):
        raise ValueError("rollback authority version 与 preview 不一致")
    if request.open_action_fingerprint != preview.get("open_action_fingerprint"):
        raise ValueError("rollback open-action fingerprint 与 preview 不一致")
    if _action_fingerprint(session, clone_task.id) != request.open_action_fingerprint:
        raise ValueError("rollback open-action fingerprint 已变化")
    if _rollback_blockers(session, clone_task.id):
        raise ValueError("rollback_blocked: Clone 已存在 Gateway-started mutation")
    legacy = _cutover_legacy_task(session, clone_task)
    authority = _clone_authority(session, clone_task, for_update=True)
    if authority.version != request.expected_authority_version:
        raise ValueError("rollback authority version 已变化")
    _handoff_to_legacy(session, authority, clone_task, legacy)
    result = {"success": True, "clone_task_id": clone_task.id, "legacy_task_id": legacy.id}
    _store_request(clone_task, "clone_rollback_apply", request, result)
    session.add(_audit(clone_task, actor_id, "rollback_applied", result))
    return result


def canonical_legacy_route(task: Task) -> dict:
    config = dict(task.type_config or {})
    if config.get("routing_rules") or config.get("dynamic_targets"):
        raise ValueError("cutover_route_scope_unsupported")
    sources = {_peer_value(item) for item in config.get("source_groups", [])}
    targets = {_peer_value(item) for item in _target_values(config)}
    sources.discard("")
    targets.discard("")
    if len(sources) != 1 or len(targets) != 1:
        raise ValueError("cutover_route_scope_unsupported")
    return {
        "source_peer_type": "channel",
        "source_peer_id": next(iter(sources)),
        "target_peer_type": "channel",
        "target_peer_id": next(iter(targets)),
    }


def _target_values(config: dict) -> list:
    values = list(config.get("target_group_ids") or [])
    if not values and config.get("target_group_id") is not None:
        values.append(config["target_group_id"])
    return values


def _peer_value(value) -> str:
    if isinstance(value, dict):
        for key in ("peer_id", "tg_peer_id", "group_id", "id"):
            if value.get(key) is not None:
                return str(value[key])
        return ""
    return str(value) if value is not None else ""


def _validate_cutover_request(task, request, preview) -> None:
    if request.legacy_task_id != task.id:
        raise ValueError("legacy_task_id 与路径不一致")
    expected = (
        (request.preview_token, preview.get("preview_token"), "preview token"),
        (request.expected_legacy_revision, task.config_revision, "legacy revision"),
        (request.route_manifest_hash, preview.get("route_manifest_hash"), "route manifest"),
        (request.open_action_fingerprint, preview.get("open_action_fingerprint"), "open actions"),
    )
    if any(actual != frozen for actual, frozen, _label in expected):
        raise ValueError("cutover preview 已变化或请求不匹配")


def _validate_clone_route(config, manifest) -> None:
    if (
        config.source.peer_type != manifest["source_peer_type"]
        or config.source.peer_id != manifest["source_peer_id"]
        or config.target.peer_type != manifest["target_peer_type"]
        or config.target.peer_id != manifest["target_peer_id"]
    ):
        raise ValueError("cutover clone config 与 legacy canonical route 不一致")


def _locked_cutover_authority(session, legacy, manifest):
    authority = session.scalar(select(TelegramGroupMutationAuthority).where(
        TelegramGroupMutationAuthority.tenant_id == legacy.tenant_id,
        TelegramGroupMutationAuthority.target_peer_type == manifest["target_peer_type"],
        TelegramGroupMutationAuthority.target_peer_id == manifest["target_peer_id"],
    ).with_for_update())
    if authority is None:
        raise RuntimeError("cutover authority missing")
    holder = session.scalar(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == authority.id,
        TelegramGroupMutationAuthorityHolder.writer_kind == "group_relay",
        TelegramGroupMutationAuthorityHolder.writer_id == legacy.id,
        TelegramGroupMutationAuthorityHolder.state == "active",
    ).with_for_update())
    if holder is None:
        raise RuntimeError("cutover legacy holder missing")
    return authority, holder


def _cutover_blockers(session, task_id, authority, holder) -> list[str]:
    states = set(session.scalars(select(Action.status).where(
        Action.task_id == task_id, Action.status.in_(UNSAFE_CUTOVER_STATES),
    )))
    holders = list(session.scalars(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == authority.id,
        TelegramGroupMutationAuthorityHolder.state == "active",
        TelegramGroupMutationAuthorityHolder.id != holder.id,
    )))
    result = [f"unsafe_action:{state}" for state in sorted(states)]
    pending = session.scalar(select(Action.id).where(
        Action.task_id == task_id,
        Action.status == "pending",
    ).limit(1))
    if pending:
        result.append("pending_action: drain legacy actions before cutover")
    result.extend(f"other_holder:{item.writer_kind}:{item.writer_id}" for item in holders)
    return result


def _freeze_clone_cutover_boundary(session, clone) -> dict[str, int]:
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == clone.id,
        CloneSourceStreamState.task_lifecycle_epoch == clone.task_lifecycle_epoch,
    ).with_for_update())
    if stream is None:
        raise RuntimeError("cutover clone stream missing")
    update_state = session.scalar(select(TelegramAuthorizationUpdateState).where(
        TelegramAuthorizationUpdateState.id == stream.authorization_update_state_id,
    ).with_for_update())
    if update_state is None or update_state.state != "live":
        raise ValueError("cutover listener update ingress not live")
    authorization = session.get(TgAccountAuthorization, stream.authorization_id)
    if authorization is None:
        raise RuntimeError("cutover listener authorization missing")
    boundary = gateway.fetch_raw_channel_boundary(
        stream.source_peer_id,
        session_ciphertext=authorization.session_ciphertext,
        credentials=credentials_for_authorization(session, authorization),
    )
    channel_pts = int(boundary.get("channel_pts") or 0)
    max_message_id = int(boundary.get("max_message_id") or 0)
    if channel_pts <= 0 or max_message_id < 0:
        raise ValueError("cutover boundary unproven")
    stream.channel_pts = channel_pts
    stream.start_pts = channel_pts
    stream.start_message_id = max_message_id
    stream.difference_cursor = {
        "start_message_id": max_message_id,
        "start_channel_pts": channel_pts,
        "cutover_ingress_order": int(update_state.last_ingress_order_no or 0),
    }
    stream.state = "catching_up"
    stream.version += 1
    subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == clone.id,
        TelegramAuthorizationUpdateSubscription.task_epoch == clone.task_lifecycle_epoch,
    ).with_for_update())
    if subscription is None:
        raise RuntimeError("cutover clone subscription missing")
    subscription.start_ingress_order = int(update_state.last_ingress_order_no or 0)
    subscription.state = "active"
    subscription.version += 1
    return {"channel_pts": channel_pts, "max_message_id": max_message_id}


def _handoff_to_clone(session, authority, old_holder, clone, manifest) -> None:
    authority.mode = "handoff"
    authority.gateway_admission_side = "new"
    authority.cutover_generation += 1
    authority.version += 1
    old_holder.holder_role = "old_handoff"
    old_holder.version += 1
    route_hash = compute_route_hash(
        manifest["source_peer_type"], manifest["source_peer_id"],
        target_peer_type=manifest["target_peer_type"],
        target_peer_id=manifest["target_peer_id"],
    )
    session.add(TelegramGroupMutationAuthorityHolder(
        authority_id=authority.id,
        writer_kind="group_clone",
        writer_id=clone.id,
        route_hash=route_hash,
        holder_role="new_handoff",
        state="active",
    ))


def _handoff_to_legacy(session, authority, clone, legacy) -> None:
    clone_holder = session.scalar(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == authority.id,
        TelegramGroupMutationAuthorityHolder.writer_kind == "group_clone",
        TelegramGroupMutationAuthorityHolder.writer_id == clone.id,
        TelegramGroupMutationAuthorityHolder.state == "active",
    ).with_for_update())
    old_holder = session.scalar(select(TelegramGroupMutationAuthorityHolder).where(
        TelegramGroupMutationAuthorityHolder.authority_id == authority.id,
        TelegramGroupMutationAuthorityHolder.writer_kind == "group_relay",
        TelegramGroupMutationAuthorityHolder.writer_id == legacy.id,
    ).with_for_update())
    if clone_holder is None or old_holder is None:
        raise RuntimeError("rollback authority holder missing")
    clone_holder.state = "released"
    clone_holder.version += 1
    old_holder.state = "active"
    old_holder.holder_role = "shared_member"
    old_holder.version += 1
    authority.mode = "shared"
    authority.gateway_admission_side = "all"
    authority.version += 1
    _stop_clone_ingress(session, clone)
    clone.status = "paused"
    clone.stats = {**dict(clone.stats or {}), "clone_start_state": "rollback_paused"}
    legacy.status = "running"
    legacy.stats = {**dict(legacy.stats or {}), "clone_start_state": "rollback_restored"}


def _stop_clone_ingress(session, clone) -> None:
    stream = session.scalar(select(CloneSourceStreamState).where(
        CloneSourceStreamState.task_id == clone.id,
        CloneSourceStreamState.task_lifecycle_epoch == clone.task_lifecycle_epoch,
    ).with_for_update())
    subscription = session.scalar(select(TelegramAuthorizationUpdateSubscription).where(
        TelegramAuthorizationUpdateSubscription.task_id == clone.id,
        TelegramAuthorizationUpdateSubscription.task_epoch == clone.task_lifecycle_epoch,
    ).with_for_update())
    if stream is None or subscription is None:
        raise RuntimeError("rollback clone ingress rows missing")
    stream.state = "stopped"
    stream.version += 1
    subscription.state = "stopped"
    subscription.version += 1


def _clone_authority(session, clone, *, for_update):
    config = dict(clone.type_config or {})
    target = dict(config.get("target") or {})
    statement = select(TelegramGroupMutationAuthority).where(
        TelegramGroupMutationAuthority.tenant_id == clone.tenant_id,
        TelegramGroupMutationAuthority.target_peer_type == target.get("peer_type"),
        TelegramGroupMutationAuthority.target_peer_id == target.get("peer_id"),
    )
    authority = session.scalar(statement.with_for_update() if for_update else statement)
    if authority is None:
        raise RuntimeError("clone cutover authority missing")
    return authority


def _cutover_legacy_task(session, clone):
    legacy_id = str((clone.stats or {}).get("cutover_legacy_task_id") or "")
    legacy = session.get(Task, legacy_id) if legacy_id else None
    if legacy is None or legacy.type != "group_relay" or legacy.tenant_id != clone.tenant_id:
        raise ValueError("clone task 不是可回滚的 cutover 任务")
    return legacy


def _rollback_blockers(session, task_id: str) -> list[str]:
    started = session.scalar(select(ExecutionAttempt.id).join(
        Action, Action.id == ExecutionAttempt.action_id,
    ).where(
        Action.task_id == task_id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1))
    if started:
        return ["clone_gateway_mutation_started"]
    observed = session.scalar(select(CloneSourceEvent.id).where(
        CloneSourceEvent.task_id == task_id,
    ).limit(1))
    if observed:
        return ["clone_source_event_observed"]
    delivered = session.scalar(select(TelegramAuthorizationUpdateDelivery.id).join(
        TelegramAuthorizationUpdateSubscription,
        TelegramAuthorizationUpdateSubscription.id
        == TelegramAuthorizationUpdateDelivery.subscription_id,
    ).where(
        TelegramAuthorizationUpdateSubscription.task_id == task_id,
    ).limit(1))
    return ["clone_source_delivery_observed"] if delivered else []


def _action_fingerprint(session, task_id: str) -> str:
    rows = session.execute(select(
        Action.id, Action.status, Action.action_version,
    ).where(
        Action.task_id == task_id,
        Action.status.in_(OPEN_ACTION_STATES),
    ).order_by(Action.id)).all()
    return _hash([list(row) for row in rows])


def _request_replay(task, key: str, request) -> dict | None:
    stored = dict((task.stats or {}).get(key) or {})
    if stored.get("client_request_id") != request.client_request_id:
        return None
    if stored.get("fingerprint") != _hash(request.model_dump(mode="json")):
        raise ValueError("client_request_id 已用于不同的 cutover/rollback 请求")
    return dict(stored.get("result") or {})


def _store_request(task, key: str, request, result: dict) -> None:
    task.stats = {
        **dict(task.stats or {}),
        key: {
            "client_request_id": request.client_request_id,
            "fingerprint": _hash(request.model_dump(mode="json")),
            "result": result,
        },
    }


def _audit(task, actor_id: int, action: str, detail: dict):
    return AuditLog(
        tenant_id=task.tenant_id,
        actor=str(actor_id),
        action=action,
        target_type="task",
        target_id=task.id,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
    )


def _hash(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


__all__ = [
    "apply_clone_cutover",
    "apply_clone_rollback",
    "canonical_legacy_route",
    "preview_clone_cutover",
    "preview_clone_rollback",
]
