from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    AccountPool,
    AccountStatus,
    TgAccount,
    TgAccountLoginBatch,
    TgAccountLoginBatchAttempt,
    TgAccountLoginBatchItem,
    TgLoginFlow,
)
from app.schemas.account_login import (
    LoginBatchCreateRequest,
    LoginBatchRefreshCredentialRequest,
    LoginBatchRetryRequest,
)
from app.security import encrypt_secret
from app.services._common import _now, audit
from app.services.account_post_login_init.policy import initialization_policy_for_pool

from .contracts import BatchLoginError
from .identity import parse_code_source_url, phone_fingerprint
from .preview import PreviewBuild, build_preview, require_batch_login_enabled, verify_preview_token


TERMINAL_ITEM_STATUSES = {"unresolved", "succeeded", "succeeded_with_warning", "failed", "skipped"}
ACTIVE_BATCH_STATUSES = ("queued", "running", "cancelling")
TERMINAL_BATCH_STATUSES = {
    "completed",
    "completed_with_manual",
    "completed_with_failures",
    "completed_with_unresolved",
    "cancelled",
}
MAX_MANUAL_RETRIES = 3


def create_login_batch(
    session: Session,
    tenant_id: int,
    user_id: int,
    actor: str,
    payload: LoginBatchCreateRequest,
) -> TgAccountLoginBatch:
    require_batch_login_enabled(session)
    request_fingerprint = _request_fingerprint(payload)
    existing = _idempotent_batch(session, tenant_id, user_id, payload.idempotency_key, request_fingerprint)
    if existing:
        return existing
    build = build_preview(session, tenant_id, payload.lines_text, payload.pool_id)
    if payload.preview_fingerprint != build.fingerprint:
        raise BatchLoginError("preview_stale", "预检输入已变化，请重新预检")
    verify_preview_token(payload.preview_token, tenant_id, user_id, build)
    decisions = _binding_decisions(payload, build)
    batch = _new_batch(tenant_id, user_id, actor, payload, request_fingerprint, build)
    pool = session.get(AccountPool, payload.pool_id)
    if not pool:
        raise BatchLoginError("pool_admission_rejected", "目标分组不存在")
    batch.initialization_policy = initialization_policy_for_pool(pool)
    try:
        with session.begin_nested():
            _persist_new_batch(session, batch, build, decisions, actor)
    except IntegrityError:
        existing = _idempotent_batch(session, tenant_id, user_id, payload.idempotency_key, request_fingerprint)
        if existing:
            return existing
        raise
    session.commit()
    session.refresh(batch)
    return batch


def _persist_new_batch(
    session: Session,
    batch: TgAccountLoginBatch,
    build: PreviewBuild,
    decisions: dict[int, tuple[bool, int | None]],
    actor: str,
) -> None:
    session.add(batch)
    session.flush()
    _add_batch_items(session, batch, build, decisions)
    audit(
        session,
        tenant_id=batch.tenant_id,
        actor=actor,
        action="创建账号批量登录",
        target_type="tg_account_login_batch",
        target_id=str(batch.id),
        detail=f"total={batch.total_count}; trace_id={batch.trace_id}; reason={batch.reason[:80]}",
    )


def _request_fingerprint(payload: LoginBatchCreateRequest) -> str:
    data = payload.model_dump(exclude={"preview_token"})
    data["reason"] = payload.reason.strip()
    raw = json.dumps(data, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def _idempotent_batch(
    session: Session,
    tenant_id: int,
    user_id: int,
    key: str,
    request_fingerprint: str,
) -> TgAccountLoginBatch | None:
    batch = session.scalar(select(TgAccountLoginBatch).where(
        TgAccountLoginBatch.tenant_id == tenant_id,
        TgAccountLoginBatch.recipient_user_id == user_id,
        TgAccountLoginBatch.idempotency_key == key,
    ))
    if batch and batch.request_fingerprint != request_fingerprint:
        raise BatchLoginError("idempotency_conflict", "幂等键已用于不同请求")
    return batch


def _binding_decisions(payload: LoginBatchCreateRequest, build: PreviewBuild) -> dict[int, tuple[bool, int | None]]:
    decisions: dict[int, tuple[bool, int | None]] = {}
    for decision in payload.binding_decisions:
        if decision.line_no in decisions:
            raise BatchLoginError("preview_stale", "同一行存在重复接码绑定决定", line_no=decision.line_no)
        decisions[decision.line_no] = (decision.replace_binding, decision.expected_binding_version)
    valid_lines = {item.line.line_no for item in build.items}
    if set(decisions) - valid_lines:
        raise BatchLoginError("preview_stale", "接码绑定决定包含不存在的行")
    for item in build.items:
        if item.output.binding_action != "replace_required":
            continue
        replace, expected = decisions.get(item.line.line_no, (False, None))
        if not replace or expected != item.output.current_binding_version:
            raise BatchLoginError("code_source_binding_conflict", "替换接码绑定需要确认当前版本", line_no=item.line.line_no)
    return decisions


def _new_batch(
    tenant_id: int,
    user_id: int,
    actor: str,
    payload: LoginBatchCreateRequest,
    request_fingerprint: str,
    build: PreviewBuild,
) -> TgAccountLoginBatch:
    return TgAccountLoginBatch(
        tenant_id=tenant_id,
        pool_id=payload.pool_id,
        created_by=actor,
        recipient_user_id=user_id,
        idempotency_key=payload.idempotency_key,
        request_fingerprint=request_fingerprint,
        total_count=len(build.items),
        reason=payload.reason.strip(),
        trace_id=uuid4().hex,
    )


def _add_batch_items(
    session: Session,
    batch: TgAccountLoginBatch,
    build: PreviewBuild,
    decisions: dict[int, tuple[bool, int | None]],
) -> None:
    settings = get_settings()
    expires_at = _now() + timedelta(seconds=settings.account_batch_login_credential_ttl_seconds)
    version = settings.account_batch_phone_fingerprint_version
    for preview in build.items:
        replace, expected = decisions.get(preview.line.line_no, (False, None))
        item = TgAccountLoginBatchItem(
            batch_id=batch.id,
            tenant_id=batch.tenant_id,
            line_no=preview.line.line_no,
            phone_masked=preview.line.phone_masked,
            phone_fingerprint=phone_fingerprint(batch.tenant_id, preview.line.phone, version),
            phone_fingerprint_version=version,
            phone_ciphertext=encrypt_secret(preview.line.phone),
            code_url_ciphertext=encrypt_secret(preview.line.source.url),
            credential_expires_at=expires_at,
            code_source_host=preview.line.source.host,
            code_source_uuid_fingerprint=preview.line.source.uuid_fingerprint,
            code_source_uuid_hint=preview.line.source.uuid_hint,
            replace_binding=replace,
            expected_binding_version=expected,
            route_hint=preview.output.route_hint,
            account_id=preview.output.account_id,
            initialization_policy=batch.initialization_policy,
        )
        session.add(item)
        session.flush()
        attempt = _new_attempt(item, batch.execution_generation, "prepare")
        session.add(attempt)
        session.flush()
        item.current_attempt_id = attempt.id


def _new_attempt(item: TgAccountLoginBatchItem, generation: int, phase: str) -> TgAccountLoginBatchAttempt:
    return TgAccountLoginBatchAttempt(
        item_id=item.id,
        batch_id=item.batch_id,
        tenant_id=item.tenant_id,
        execution_generation=generation,
        phase=phase,
    )


def list_login_batches(session: Session, tenant_id: int, *, limit: int, offset: int) -> list[TgAccountLoginBatch]:
    return list(session.scalars(select(TgAccountLoginBatch).where(
        TgAccountLoginBatch.tenant_id == tenant_id,
    ).order_by(
        case((TgAccountLoginBatch.status.in_(ACTIVE_BATCH_STATUSES), 0), else_=1),
        TgAccountLoginBatch.id.desc(),
    ).offset(offset).limit(limit)))


def get_login_batch(session: Session, tenant_id: int, batch_id: int) -> TgAccountLoginBatch:
    batch = session.get(TgAccountLoginBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id:
        raise BatchLoginError("not_found", "批量登录任务不存在")
    return batch


def get_login_batch_items(
    session: Session,
    tenant_id: int,
    batch_id: int,
    *,
    limit: int,
    offset: int,
) -> list[TgAccountLoginBatchItem]:
    get_login_batch(session, tenant_id, batch_id)
    return list(session.scalars(select(TgAccountLoginBatchItem).where(
        TgAccountLoginBatchItem.batch_id == batch_id,
        TgAccountLoginBatchItem.tenant_id == tenant_id,
    ).order_by(TgAccountLoginBatchItem.line_no).offset(offset).limit(limit)))


def cancel_login_batch(
    session: Session,
    tenant_id: int,
    batch_id: int,
    expected_version: int,
    actor: str,
    reason: str,
) -> TgAccountLoginBatch:
    batch = session.scalar(select(TgAccountLoginBatch).where(TgAccountLoginBatch.id == batch_id).with_for_update())
    if not batch or batch.tenant_id != tenant_id:
        raise BatchLoginError("not_found", "批量登录任务不存在")
    replay_versions = {batch.state_version - 1, batch.state_version - 2}
    if batch.status in {"cancelling", "cancelled"} and expected_version in replay_versions:
        return batch
    if batch.state_version != expected_version:
        raise BatchLoginError("state_conflict", "批次状态已变化")
    if batch.status in TERMINAL_BATCH_STATUSES:
        return batch
    batch.status = "cancelling"
    batch.state_version += 1
    skip_cancellable_items(session, batch.id)
    audit(session, tenant_id=tenant_id, actor=actor, action="取消账号批量登录", target_type="tg_account_login_batch", target_id=str(batch.id), detail=f"reason={reason.strip()[:80]}")
    from .notifications import finalize_batch_if_terminal

    finalize_batch_if_terminal(session, batch.id)
    session.commit()
    session.refresh(batch)
    return batch


def skip_cancellable_items(session: Session, batch_id: int) -> None:
    items = session.scalars(select(TgAccountLoginBatchItem).where(
        TgAccountLoginBatchItem.batch_id == batch_id,
        TgAccountLoginBatchItem.status.in_(("pending", "waiting", "post_initialization_waiting")),
    ).with_for_update())
    for item in items:
        was_post_initialization = item.status == "post_initialization_waiting"
        attempt = (
            session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
            if item.current_attempt_id
            else None
        )
        if attempt and any(value == "started" for value in (
            attempt.send_call_state, attempt.code_verify_call_state, attempt.twofa_verify_call_state,
        )):
            continue
        account = session.get(TgAccount, item.account_id) if item.account_id else None
        if item.phase in {"pool_transition", "online_readback"} and account and account.status == AccountStatus.ACTIVE.value:
            continue
        item.status = "skipped"
        item.failure_type = "manual_interrupted"
        item.failure_detail = "批次已取消，未开始行已跳过"
        item.finished_at = _now()
        if not was_post_initialization:
            item.code_url_ciphertext = None
        item.state_version += 1
        if attempt:
            attempt.phase = "skipped"
            attempt.lease_token = ""
            attempt.lease_expires_at = None
            attempt.state_version += 1
            _cancel_batch_flow(session, item, attempt)


def _cancel_batch_flow(session: Session, item: TgAccountLoginBatchItem, attempt: TgAccountLoginBatchAttempt) -> None:
    flow = session.get(TgLoginFlow, attempt.flow_id) if attempt.flow_id else None
    if not flow or flow.batch_login_attempt_id != attempt.id:
        return
    flow.status = "已取消"
    flow.flow_version += 1
    flow.code_preview = None
    flow.temporary_session_ciphertext = None
    flow.phone_code_hash_ciphertext = None
    flow.failure_type = "manual_interrupted"
    flow.failure_detail = "批量登录已由操作员取消"
    account = session.get(TgAccount, item.account_id) if item.account_id else None
    if account and account.status != AccountStatus.ACTIVE.value:
        account.status = AccountStatus.NEED_RELOGIN.value


def retry_login_batch_items(
    session: Session,
    tenant_id: int,
    batch_id: int,
    payload: LoginBatchRetryRequest,
    actor: str,
) -> TgAccountLoginBatch:
    require_batch_login_enabled(session)
    batch = _locked_batch(session, tenant_id, batch_id, payload.expected_state_version)
    items = _retry_items(session, batch, payload.item_ids)
    _validate_unresolved_retry(session, batch, items, payload)
    for item in items:
        _reset_item_for_retry(session, item)
    batch.status = "queued"
    batch.execution_generation = max(item.execution_generation for item in items)
    batch.state_version += 1
    batch.finished_at = None
    audit(session, tenant_id=tenant_id, actor=actor, action="重试账号批量登录行", target_type="tg_account_login_batch", target_id=str(batch.id), detail=f"items={len(items)}; reason={payload.reason[:80]}")
    session.commit()
    session.refresh(batch)
    return batch


def _locked_batch(session: Session, tenant_id: int, batch_id: int, expected_version: int) -> TgAccountLoginBatch:
    batch = session.scalar(select(TgAccountLoginBatch).where(TgAccountLoginBatch.id == batch_id).with_for_update())
    if not batch or batch.tenant_id != tenant_id:
        raise BatchLoginError("not_found", "批量登录任务不存在")
    if batch.state_version != expected_version:
        raise BatchLoginError("state_conflict", "批次状态已变化")
    return batch


def _retry_items(session: Session, batch: TgAccountLoginBatch, item_ids: list[int] | None) -> list[TgAccountLoginBatchItem]:
    query = select(TgAccountLoginBatchItem).where(
        TgAccountLoginBatchItem.batch_id == batch.id,
        TgAccountLoginBatchItem.status.in_(("failed", "unresolved")),
    )
    if item_ids:
        query = query.where(TgAccountLoginBatchItem.id.in_(item_ids))
    items = list(session.scalars(query.order_by(TgAccountLoginBatchItem.line_no).with_for_update()))
    if not items:
        raise BatchLoginError("state_conflict", "没有可重试的失败或未解行")
    if any(_is_post_init_failure(item) for item in items):
        raise BatchLoginError(
            "post_init_action_required",
            "账号已授权，请使用完整初始化专项操作收口，不能重试整条登录",
        )
    if any(item.retry_count >= MAX_MANUAL_RETRIES for item in items):
        raise BatchLoginError("retry_limit_exceeded", "行重试次数已达到上限")
    if any(not item.code_url_ciphertext or not item.credential_expires_at or item.credential_expires_at <= _now() for item in items):
        raise BatchLoginError("credential_expired", "请先刷新已过期的接码地址")
    return items


def _is_post_init_failure(item: TgAccountLoginBatchItem) -> bool:
    return bool(
        item.initialization_policy == "normal_full_init_v1"
        and item.authorization_status == "confirmed"
        and item.post_initialization_status != "succeeded"
    )


def _validate_unresolved_retry(session: Session, batch: TgAccountLoginBatch, items: list[TgAccountLoginBatchItem], payload: LoginBatchRetryRequest) -> None:
    unresolved = [item for item in items if item.status == "unresolved"]
    if not unresolved:
        return
    if len(items) != 1 or not payload.confirm_remote_unknown:
        raise BatchLoginError("state_conflict", "未解行必须单独显式确认远程未知后重试")
    item = unresolved[0]
    attempt = session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
    expected = (attempt.id, attempt.state_version, batch.resolution_version) if attempt else (None, None, None)
    provided = (payload.expected_attempt_id, payload.expected_attempt_version, payload.expected_resolution_version)
    reconciliation_complete = bool(attempt and (
        attempt.reconcile_status in {"exhausted", "manual_review_required"}
        or (attempt.reconcile_status == "pending" and attempt.last_reconciled_at)
    ))
    if provided != expected or not reconciliation_complete:
        raise BatchLoginError("state_conflict", "未解行对账状态已变化")


def _reset_item_for_retry(session: Session, item: TgAccountLoginBatchItem) -> None:
    _supersede_previous_flow(session, item)
    item.execution_generation += 1
    item.retry_count += 1
    item.status = "pending"
    item.phase = "pool_transition" if item.failure_type == "pool_transition_failed" else "prepare"
    item.failure_type = ""
    item.failure_detail = ""
    item.warning_detail = ""
    item.finished_at = None
    item.next_retry_at = None
    item.state_version += 1
    attempt = _new_attempt(item, item.execution_generation, item.phase)
    session.add(attempt)
    session.flush()
    item.current_attempt_id = attempt.id


def _supersede_previous_flow(session: Session, item: TgAccountLoginBatchItem) -> None:
    previous = session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
    if not previous:
        return
    if previous.reconcile_status in {"pending", "probing", "exhausted", "manual_review_required"}:
        previous.reconcile_status = "superseded"
        previous.state_version += 1
    flow = session.get(TgLoginFlow, previous.flow_id) if previous.flow_id else None
    if not flow or flow.batch_login_attempt_id != previous.id or flow.status == AccountStatus.ACTIVE.value:
        return
    flow.status = "superseded"
    flow.flow_version += 1
    flow.code_preview = None
    flow.temporary_session_ciphertext = None
    flow.phone_code_hash_ciphertext = None


def refresh_login_item_credential(
    session: Session,
    tenant_id: int,
    batch_id: int,
    item_id: int,
    payload: LoginBatchRefreshCredentialRequest,
    actor: str,
) -> TgAccountLoginBatchItem:
    require_batch_login_enabled(session)
    spec = parse_code_source_url(payload.code_url)
    item = session.scalar(select(TgAccountLoginBatchItem).where(
        TgAccountLoginBatchItem.id == item_id,
        TgAccountLoginBatchItem.batch_id == batch_id,
    ).with_for_update())
    if not item or item.tenant_id != tenant_id:
        raise BatchLoginError("not_found", "批量登录行不存在")
    if item.state_version != payload.expected_item_version:
        raise BatchLoginError("state_conflict", "行状态已变化")
    if spec.uuid_fingerprint != item.code_source_uuid_fingerprint and not payload.replace_binding:
        raise BatchLoginError("code_source_binding_conflict", "更换 UUID 需要显式确认替换绑定")
    account = session.get(TgAccount, item.account_id) if item.account_id else None
    replacing = spec.uuid_fingerprint != item.code_source_uuid_fingerprint
    bound = session.scalar(select(TgAccount.id).where(
        TgAccount.tenant_id == item.tenant_id,
        TgAccount.code_source_uuid_fingerprint == spec.uuid_fingerprint,
        TgAccount.id != item.account_id,
    ))
    if bound:
        raise BatchLoginError("code_source_binding_conflict", "UUID 已绑定其他账号")
    if replacing and account and payload.expected_binding_version != account.code_source_binding_version:
        raise BatchLoginError("code_source_binding_conflict", "账号接码绑定版本已变化")
    if replacing and not account and payload.expected_binding_version not in {None, 0}:
        raise BatchLoginError("code_source_binding_conflict", "接码绑定版本已变化")
    item.code_url_ciphertext = encrypt_secret(spec.url)
    item.credential_expires_at = _now() + timedelta(seconds=get_settings().account_batch_login_credential_ttl_seconds)
    item.code_source_host = spec.host
    item.code_source_uuid_fingerprint = spec.uuid_fingerprint
    item.code_source_uuid_hint = spec.uuid_hint
    item.replace_binding = payload.replace_binding
    item.expected_binding_version = payload.expected_binding_version
    item.state_version += 1
    audit(session, tenant_id=tenant_id, actor=actor, action="刷新批量登录接码凭据", target_type="tg_account_login_batch_item", target_id=str(item.id), detail=f"batch_id={batch_id}; reason={payload.reason[:80]}")
    session.commit()
    session.refresh(item)
    return item


__all__ = [
    "TERMINAL_ITEM_STATUSES",
    "TERMINAL_BATCH_STATUSES",
    "cancel_login_batch",
    "create_login_batch",
    "get_login_batch",
    "get_login_batch_items",
    "list_login_batches",
    "refresh_login_item_credential",
    "retry_login_batch_items",
    "skip_cancellable_items",
]
