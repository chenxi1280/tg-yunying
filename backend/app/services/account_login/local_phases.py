from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.models import AccountPool, AccountStatus, TgAccount, TgAccountLoginBatch, TgLoginFlow
from app.services._common import _now, audit
from app.services.account_usage_policy import sync_account_usage
from app.services.developer_apps import credentials_for_account
from app.timezone import as_beijing_aware

from .binding import bind_account_code_source, bind_or_create_account
from .contracts import BatchLoginError
from .state import PhaseClaim, advance_claim, commit_claim, fail_claim, load_claim


OPEN_FLOW_STATUSES = {"intent_persisted", "challenge_sent", "等待验证码", "等待扫码", "等待2FA"}


def execute_local_phase(session, claim: PhaseClaim) -> None:
    try:
        handlers = {
            "prepare": _prepare,
            "bind_account": _bind_account,
            "bind_code_source": _bind_code_source,
            "acquire_flow": _acquire_flow,
            "pool_transition": _pool_transition,
        }
        handler = handlers.get(claim.phase)
        if not handler:
            raise RuntimeError(f"unsupported local batch login phase: {claim.phase}")
        handler(session, claim)
    except BatchLoginError as exc:
        fail_claim(session, claim, exc.code, str(exc))
    except Exception as exc:
        failure = "pool_transition_failed" if claim.phase == "pool_transition" else "account_create_failed"
        fail_claim(session, claim, failure, _safe_detail(exc, claim.phase))
    commit_claim(session)


def _prepare(session, claim: PhaseClaim) -> None:
    item, _ = load_claim(session, claim)
    if item.account_id:
        account = session.get(TgAccount, item.account_id)
        if not account or account.tenant_id != item.tenant_id or account.deleted_at is not None:
            raise BatchLoginError("code_source_binding_conflict", "预检账号已不可用")
        advance_claim(session, claim, "authorization_probe")
        return
    advance_claim(session, claim, "code_baseline")


def _bind_account(session, claim: PhaseClaim) -> None:
    item, _ = load_claim(session, claim)
    login_batch = session.get(TgAccountLoginBatch, item.batch_id)
    if not login_batch:
        raise BatchLoginError("account_create_failed", "批次不存在")
    result = bind_or_create_account(session, item, login_batch.pool_id, "account-login-worker")
    item.route = "new_account" if result.created else "existing_probe_required"
    advance_claim(session, claim, "bind_code_source" if result.created else "authorization_probe")


def _bind_code_source(session, claim: PhaseClaim) -> None:
    item, _ = load_claim(session, claim)
    account = _item_account(session, item)
    verified = item.route != "already_authorized"
    bind_account_code_source(session, account, item, "account-login-worker", verified=verified)
    advance_claim(session, claim, "pool_transition" if item.route == "already_authorized" else "acquire_flow")


def _acquire_flow(session, claim: PhaseClaim) -> None:
    item, attempt = load_claim(session, claim)
    account = _item_account(session, item)
    existing = session.scalar(select(TgLoginFlow).where(
        TgLoginFlow.account_id == account.id,
        TgLoginFlow.authorization_role == "primary",
        TgLoginFlow.status.in_(OPEN_FLOW_STATUSES),
    ).order_by(TgLoginFlow.id.desc()).limit(1).with_for_update())
    if existing and existing.batch_login_attempt_id != attempt.id:
        _wait_for_flow_owner(session, claim, item, attempt)
        return
    credentials_for_account(session, account, assign_if_missing=True)
    flow = existing or TgLoginFlow(
        tenant_id=account.tenant_id,
        account_id=account.id,
        method="code",
        status="intent_persisted",
        authorization_role="primary",
        developer_app_id=account.developer_app_id,
        proxy_id=account.proxy_id,
        batch_login_attempt_id=attempt.id,
        batch_login_generation=item.execution_generation,
    )
    if not existing:
        session.add(flow)
        session.flush()
    account.status = "intent_persisted"
    item.failure_type = ""
    item.failure_detail = ""
    attempt.flow_id = flow.id
    attempt.flow_version = flow.flow_version
    audit(session, tenant_id=item.tenant_id, actor="account-login-worker", action="创建批量登录专属flow", target_type="tg_account_login_batch_item", target_id=str(item.id), detail=f"flow_id={flow.id}; generation={item.execution_generation}")
    advance_claim(session, claim, "send_code")


def _wait_for_flow_owner(session, claim: PhaseClaim, item, attempt) -> None:
    now = _now()
    if attempt.deadline_at and as_beijing_aware(now) >= as_beijing_aware(attempt.deadline_at):
        raise BatchLoginError("item_deadline_exceeded", "等待其他登录流程超过单行预算")
    item.failure_type = "login_flow_conflict"
    item.failure_detail = "账号已有其他登录流程，等待其结束"
    retry_at = now + timedelta(seconds=get_settings().account_batch_login_poll_interval_seconds)
    advance_claim(session, claim, "acquire_flow", status="waiting", next_retry_at=retry_at)


def _pool_transition(session, claim: PhaseClaim) -> None:
    item, _ = load_claim(session, claim)
    account = _item_account(session, item)
    if account.status != AccountStatus.ACTIVE.value or not account.session_ciphertext:
        raise BatchLoginError("login_remote_not_completed", "账号授权尚未完成")
    batch = session.get(TgAccountLoginBatch, item.batch_id)
    pool = session.get(AccountPool, batch.pool_id) if batch else None
    if not batch or not pool or pool.tenant_id != item.tenant_id:
        raise BatchLoginError("pool_transition_failed", "目标分组不存在")
    sync_account_usage(session, account, pool, "account-login-worker")
    audit(session, tenant_id=item.tenant_id, actor="account-login-worker", action="批量登录账号进入目标分组", target_type="tg_account", target_id=str(account.id), detail=f"batch_id={batch.id}; pool_id={pool.id}")
    advance_claim(session, claim, "online_readback")


def _item_account(session, item) -> TgAccount:
    account = session.get(TgAccount, item.account_id)
    if not account or account.tenant_id != item.tenant_id or account.deleted_at is not None:
        raise BatchLoginError("account_create_failed", "批量行账号不存在")
    return account


def _safe_detail(exc: Exception, phase: str) -> str:
    return f"{phase} failed: {exc.__class__.__name__}"[:300]


__all__ = ["execute_local_phase"]
