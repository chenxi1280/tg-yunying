from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountProxy,
    AccountProxyBinding,
    AccountStatus,
    TelegramDeveloperApp,
    TgAccount,
    TgAccountAuthorization,
    TgLoginFlow,
    TgVerificationCode,
)
from app.security import encrypt_secret, encrypt_session

from ._common import _is_expired, _now, audit, gateway, get_account_phone
from .account_authorization_constants import (
    ACTIVE_STATUSES,
    HEALTHY_PROXY_ALERT_STATUSES,
    HEALTHY_PROXY_STATUSES,
    NEEDS_REPAIR_STATUS,
    PRIMARY_ROLE,
    PROXY_FAILURE_MARKERS,
    REPAIR_ROLE,
    STANDBY_ROLES,
)
from .account_authorization_read_model import (
    authorization_summaries_for_accounts,
    authorization_summary_for_account,
    list_account_authorizations,
)
from .account_authorization_metadata import read_authorization_metadata
from .account_two_fa import rotate_managed_two_fa_after_login
from .developer_apps import credentials_for_developer_app


def is_proxy_recovery_signal(detail: str) -> bool:
    text = str(detail or "").lower()
    return any(marker.lower() in text for marker in PROXY_FAILURE_MARKERS)


def start_standby_authorization_login(
    session: Session,
    account_id: int,
    *,
    method: str,
    role: str,
    developer_app_id: int,
    proxy_id: int,
    actor: str,
) -> TgLoginFlow:
    account = _require_account(session, account_id)
    app, proxy = _require_login_resources(session, account.tenant_id, role, developer_app_id, proxy_id)
    credentials = credentials_for_developer_app(app, proxy)
    challenge = gateway.start_login(method, account_id=account.id, phone=get_account_phone(account), credentials=credentials)
    flow = _standby_login_flow(account, challenge, method=method, role=role, developer_app_id=app.id, proxy_id=proxy.id)
    session.add(flow)
    _record_login_code_if_present(session, account, challenge)
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="开始备用授权登录",
        target_type="tg_account",
        target_id=str(account.id),
        detail=f"method={method}; role={role}; developer_app_id={app.id}; proxy_id={proxy.id}",
    )
    session.commit()
    session.refresh(flow)
    return flow


def verify_standby_authorization_login(
    session: Session,
    account_id: int,
    flow_id: int,
    *,
    code: str | None,
    password_2fa: str | None,
    actor: str,
) -> TgAccountAuthorization:
    account = _require_account(session, account_id)
    flow = _require_standby_login_flow(session, account, flow_id)
    _expire_flow_if_needed(session, account, flow, actor, password_2fa)
    app = _require_developer_app(session, flow.developer_app_id)
    proxy = _require_proxy(session, account.tenant_id, flow.proxy_id)
    credentials = credentials_for_developer_app(app, proxy)
    status, raw_session = gateway.finish_login(
        code,
        password_2fa,
        account_id=account.id,
        phone=get_account_phone(account),
        credentials=credentials,
    )
    asset = _finish_standby_login(session, account, flow, status, raw_session, actor)
    if password_2fa:
        rotate_managed_two_fa_after_login(
            session,
            account,
            session_ciphertext=asset.session_ciphertext,
            current_password=password_2fa,
            credentials=credentials,
            telegram_gateway=gateway,
            marker=f"standby-{asset.id}",
        )
        session.commit()
        session.refresh(asset)
    return asset


def check_standby_authorization_qr_login(session: Session, account_id: int, flow_id: int, *, actor: str) -> TgAccountAuthorization:
    account = _require_account(session, account_id)
    flow = _require_standby_login_flow(session, account, flow_id)
    app = _require_developer_app(session, flow.developer_app_id)
    proxy = _require_proxy(session, account.tenant_id, flow.proxy_id)
    status, raw_session = gateway.finish_login(
        "qr-confirmed",
        None,
        account_id=account.id,
        phone=get_account_phone(account),
        credentials=credentials_for_developer_app(app, proxy),
    )
    return _finish_standby_login(session, account, flow, status, raw_session, actor)


def attempt_standby_authorization_recovery(
    session: Session,
    account: TgAccount,
    *,
    actor: str,
    reason: str,
) -> TgAccountAuthorization | None:
    standby = _first_switchable_standby(session, account)
    if standby is None:
        return None
    switch_primary_authorization(session, account.id, standby.id, actor=actor, reason=reason)
    session.refresh(standby)
    return standby


def attempt_primary_proxy_recovery(
    session: Session,
    account: TgAccount,
    *,
    actor: str,
    reason: str,
) -> AccountProxy | None:
    if not account.session_ciphertext:
        return None
    proxy = _first_recovery_proxy(session, account)
    if proxy is None:
        return None
    old_proxy_id = account.proxy_id
    account.proxy_id = proxy.id
    _update_current_authorization_proxy(session, account, proxy.id)
    session.add(
        AccountProxyBinding(
            tenant_id=account.tenant_id,
            account_id=account.id,
            proxy_id=proxy.id,
            change_reason=f"自动换线复用主 session：{reason}",
            bound_by=actor,
        )
    )
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="自动切换账号代理",
        target_type="tg_account",
        target_id=str(account.id),
        detail=f"old_proxy_id={old_proxy_id}; new_proxy_id={proxy.id}; reason={reason}",
    )
    session.commit()
    session.refresh(account)
    return proxy


def switch_primary_authorization(
    session: Session,
    account_id: int,
    authorization_id: int,
    *,
    actor: str,
    reason: str,
) -> TgAccount:
    account = _require_account(session, account_id)
    target = _require_authorization(session, account, authorization_id)
    _ensure_switchable(target)
    _preserve_legacy_primary_if_needed(session, account, reason)
    _demote_current_authorizations(session, account, authorization_id, reason)
    _promote_authorization(account, target)
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="切换账号主授权",
        target_type="tg_account",
        target_id=str(account.id),
        detail=f"authorization_id={authorization_id}; reason={reason}",
    )
    session.commit()
    session.refresh(account)
    return account


def activate_authorization(
    session: Session,
    account_id: int,
    authorization_id: int,
    *,
    actor: str,
    reason: str,
) -> TgAccount:
    return switch_primary_authorization(session, account_id, authorization_id, actor=actor, reason=reason)


def refresh_authorization_slot(
    session: Session,
    account_id: int,
    authorization_id: int,
    *,
    actor: str,
    reason: str,
) -> dict[str, Any]:
    account = _require_account(session, account_id)
    target = _require_authorization(session, account, authorization_id)
    source = _first_healthy_authorization(session, account, exclude_id=target.id)
    if source is None:
        _mark_all_down_manual_required(session, account, reason)
        session.commit()
        raise ValueError("三槽位全部掉线，只能人工重新登录 / 扫码 / 手动验证码")
    target.status = "refreshing"
    target.derived_status = "waiting_code"
    target.failure_reason = f"等待健康槽位读取 Telegram 官方验证码：{reason}"
    target.last_health_check_at = _now()
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="刷新账号授权槽位",
        target_type="tg_account_authorization",
        target_id=str(target.id),
        detail=f"source_authorization_id={source.id}; target_role={target.role}; reason={reason}",
    )
    session.commit()
    return {
        "account_id": account.id,
        "authorization_id": target.id,
        "status": "waiting_code",
        "target_role": target.role,
        "source_authorization_id": source.id,
        "source_role": source.role,
        "next_action": "manual_code_or_auto_code_poll",
        "detail": target.failure_reason,
    }


def self_heal_authorizations(session: Session, account_id: int, *, actor: str, reason: str) -> dict[str, Any]:
    account = _require_account(session, account_id)
    standby = _first_switchable_standby(session, account)
    if standby is None:
        _mark_all_down_manual_required(session, account, reason)
        session.commit()
        return {
            "account_id": account.id,
            "status": "manual_required",
            "activated_authorization_id": None,
            "refresh_authorization_id": None,
            "next_action": "manual_login_or_qr_or_code",
            "detail": "三槽位全部掉线，只能人工重新登录 / 扫码 / 手动验证码",
        }
    switch_primary_authorization(session, account.id, standby.id, actor=actor, reason=reason)
    return {
        "account_id": account.id,
        "status": "activated_standby",
        "activated_authorization_id": standby.id,
        "refresh_authorization_id": None,
        "next_action": "refresh_previous_primary",
        "detail": "已激活健康备用授权，原主授权保留为待修复资产",
    }


def _authorization_rows(session: Session, account: TgAccount) -> list[TgAccountAuthorization]:
    return list(
        session.scalars(
            select(TgAccountAuthorization)
            .where(TgAccountAuthorization.account_id == account.id, TgAccountAuthorization.disabled_at.is_(None))
            .order_by(TgAccountAuthorization.is_current.desc(), TgAccountAuthorization.id.asc())
        )
    )


def _require_login_resources(
    session: Session,
    tenant_id: int,
    role: str,
    developer_app_id: int,
    proxy_id: int,
) -> tuple[TelegramDeveloperApp, AccountProxy]:
    if role not in STANDBY_ROLES:
        raise ValueError("备用授权角色无效")
    app = _require_developer_app(session, developer_app_id)
    proxy = session.get(AccountProxy, proxy_id)
    if not proxy or proxy.tenant_id != tenant_id:
        raise ValueError("备用授权代理不存在")
    if proxy.status not in HEALTHY_PROXY_STATUSES:
        raise ValueError("备用授权代理不可用")
    return app, proxy


def _require_developer_app(session: Session, developer_app_id: int | None) -> TelegramDeveloperApp:
    if developer_app_id is None:
        raise ValueError("备用授权缺少开发者应用")
    app = session.get(TelegramDeveloperApp, developer_app_id)
    if not app:
        raise ValueError("备用授权开发者应用不存在")
    return app


def _require_proxy(session: Session, tenant_id: int, proxy_id: int | None) -> AccountProxy:
    if proxy_id is None:
        raise ValueError("备用授权缺少代理")
    proxy = session.get(AccountProxy, proxy_id)
    if not proxy or proxy.tenant_id != tenant_id:
        raise ValueError("备用授权代理不存在")
    return proxy


def _standby_login_flow(
    account: TgAccount,
    challenge: Any,
    *,
    method: str,
    role: str,
    developer_app_id: int,
    proxy_id: int,
) -> TgLoginFlow:
    return TgLoginFlow(
        tenant_id=account.tenant_id,
        account_id=account.id,
        method=method,
        status=challenge.status,
        code_preview=challenge.code_preview,
        code_expires_at=challenge.code_expires_at,
        qr_payload=challenge.qr_payload,
        authorization_role=role,
        developer_app_id=developer_app_id,
        proxy_id=proxy_id,
    )


def _record_login_code_if_present(session: Session, account: TgAccount, challenge: Any) -> None:
    if not challenge.code_preview:
        return
    session.add(
        TgVerificationCode(
            tenant_id=account.tenant_id,
            account_id=account.id,
            source="authorization_login_flow",
            code_preview=challenge.code_preview,
            expires_at=challenge.code_expires_at,
            raw_hint="平台发起备用授权登录验证码",
        )
    )


def _require_standby_login_flow(session: Session, account: TgAccount, flow_id: int) -> TgLoginFlow:
    flow = session.get(TgLoginFlow, flow_id)
    if not flow or flow.account_id != account.id or flow.authorization_role not in STANDBY_ROLES:
        raise ValueError("备用授权登录流水不存在")
    return flow


def _expire_flow_if_needed(
    session: Session,
    account: TgAccount,
    flow: TgLoginFlow,
    actor: str,
    password_2fa: str | None,
) -> None:
    if not flow.code_preview or not _is_expired(flow.code_expires_at) or password_2fa:
        return
    flow.code_preview = None
    flow.status = "已过期"
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="备用授权登录失败",
        target_type="tg_account",
        target_id=str(account.id),
        detail="code expired",
    )
    session.commit()
    raise ValueError("备用授权登录验证码已过期")


def _finish_standby_login(
    session: Session,
    account: TgAccount,
    flow: TgLoginFlow,
    status: str,
    raw_session: str,
    actor: str,
) -> TgAccountAuthorization:
    flow.status = status
    flow.code_preview = None
    if status != AccountStatus.ACTIVE.value or not raw_session:
        session.commit()
        raise ValueError(f"备用授权登录未完成：{status}")
    _mark_same_role_for_repair(session, account, flow)
    app = _require_developer_app(session, flow.developer_app_id)
    encrypted_session = encrypt_session(raw_session)
    authorization_hash = _current_authorization_hash_after_login(session, account, flow, app, encrypted_session)
    asset = TgAccountAuthorization(
        tenant_id=account.tenant_id,
        account_id=account.id,
        role=flow.authorization_role,
        developer_app_id=flow.developer_app_id,
        developer_app_api_id_snapshot=app.api_id,
        proxy_id=flow.proxy_id,
        session_ciphertext=encrypted_session,
        telegram_authorization_hash_ciphertext=encrypt_secret(authorization_hash),
        status="standby",
        health_status="healthy",
        is_current=False,
        last_success_at=_now(),
        created_by=actor,
    )
    session.add(asset)
    session.flush()
    flow.authorization_id = asset.id
    audit(
        session,
        tenant_id=account.tenant_id,
        actor=actor,
        action="完成备用授权登录",
        target_type="tg_account",
        target_id=str(account.id),
        detail=f"role={asset.role}; authorization_id={asset.id}",
    )
    session.commit()
    session.refresh(asset)
    return asset


def _mark_same_role_for_repair(session: Session, account: TgAccount, flow: TgLoginFlow) -> None:
    rows = _authorization_rows(session, account)
    for row in rows:
        if row.role != flow.authorization_role:
            continue
        row.status = NEEDS_REPAIR_STATUS
        row.failure_reason = "同角色备用授权已重新登录，旧授权待确认后停用"


def _current_authorization_hash_after_login(
    session: Session,
    account: TgAccount,
    flow: TgLoginFlow,
    app: TelegramDeveloperApp,
    session_ciphertext: str,
) -> str:
    proxy = _require_proxy(session, account.tenant_id, flow.proxy_id) if flow.proxy_id else None
    metadata = read_authorization_metadata(
        session,
        account=account,
        app=app,
        proxy=proxy,
        session_ciphertext=session_ciphertext,
    )
    return metadata.authorization_hash


def _primary_row(rows: list[TgAccountAuthorization]) -> TgAccountAuthorization | None:
    for row in rows:
        if row.is_current or row.role == PRIMARY_ROLE:
            return row
    return rows[0] if rows else None


def _is_healthy_standby(row: TgAccountAuthorization) -> bool:
    return row.role in STANDBY_ROLES and row.status in ACTIVE_STATUSES and bool(row.session_ciphertext)


def _has_explicit_primary(rows: list[TgAccountAuthorization]) -> bool:
    return any(row.is_current or row.role == PRIMARY_ROLE for row in rows)


def _require_account(session: Session, account_id: int) -> TgAccount:
    account = session.get(TgAccount, account_id)
    if not account or account.deleted_at is not None:
        raise ValueError("account not found")
    return account


def _require_authorization(session: Session, account: TgAccount, authorization_id: int) -> TgAccountAuthorization:
    authorization = session.get(TgAccountAuthorization, authorization_id)
    if not authorization or authorization.account_id != account.id or authorization.disabled_at is not None:
        raise ValueError("authorization not found")
    return authorization


def _ensure_switchable(authorization: TgAccountAuthorization) -> None:
    if not authorization.session_ciphertext:
        raise ValueError("备用授权没有可用 session")
    if authorization.status not in ACTIVE_STATUSES:
        raise ValueError("备用授权状态不可切换")


def _first_switchable_standby(session: Session, account: TgAccount) -> TgAccountAuthorization | None:
    rows = _authorization_rows(session, account)
    for row in rows:
        if _is_healthy_standby(row):
            return row
    return None


def _first_healthy_authorization(session: Session, account: TgAccount, *, exclude_id: int) -> TgAccountAuthorization | None:
    for row in _authorization_rows(session, account):
        if row.id == exclude_id:
            continue
        if not row.session_ciphertext:
            continue
        if row.status not in ACTIVE_STATUSES:
            continue
        if row.health_status not in {"healthy", "legacy", ""}:
            continue
        return row
    return None


def _mark_all_down_manual_required(session: Session, account: TgAccount, reason: str) -> None:
    for row in _authorization_rows(session, account):
        row.derived_status = "manual_required"
        row.failure_reason = f"三槽位全部掉线，只能人工重新登录 / 扫码 / 手动验证码：{reason}"
    account.status = AccountStatus.NEED_RELOGIN.value


def _first_recovery_proxy(session: Session, account: TgAccount) -> AccountProxy | None:
    rows = session.execute(
        select(AccountProxy, func.count(TgAccount.id).label("bound_count"))
        .outerjoin(TgAccount, TgAccount.proxy_id == AccountProxy.id)
        .where(
            AccountProxy.tenant_id == account.tenant_id,
            AccountProxy.id != account.proxy_id,
            AccountProxy.status.in_(HEALTHY_PROXY_STATUSES),
            AccountProxy.alert_status.in_(HEALTHY_PROXY_ALERT_STATUSES),
        )
        .group_by(AccountProxy.id)
        .order_by(func.count(TgAccount.id).asc(), AccountProxy.id.asc())
    )
    for proxy, bound_count in rows:
        if proxy.max_bound_accounts > 0 and int(bound_count or 0) >= proxy.max_bound_accounts:
            continue
        return proxy
    return None


def _update_current_authorization_proxy(session: Session, account: TgAccount, proxy_id: int) -> None:
    rows = _authorization_rows(session, account)
    primary = _primary_row(rows)
    if primary is not None:
        primary.proxy_id = proxy_id


def _preserve_legacy_primary_if_needed(session: Session, account: TgAccount, reason: str) -> None:
    if not account.session_ciphertext or _has_explicit_primary(_authorization_rows(session, account)):
        return
    session.add(
        TgAccountAuthorization(
            tenant_id=account.tenant_id,
            account_id=account.id,
            role=REPAIR_ROLE,
            developer_app_id=account.developer_app_id,
            proxy_id=account.proxy_id,
            session_ciphertext=account.session_ciphertext,
            status=NEEDS_REPAIR_STATUS,
            health_status="unknown",
            failure_reason=f"切换主授权后保留待修复：{reason}",
        )
    )
    session.flush()


def _demote_current_authorizations(session: Session, account: TgAccount, target_id: int, reason: str) -> None:
    rows = _authorization_rows(session, account)
    for row in rows:
        if row.id == target_id or not (row.is_current or row.role == PRIMARY_ROLE):
            continue
        row.is_current = False
        row.role = REPAIR_ROLE
        row.status = NEEDS_REPAIR_STATUS
        row.failure_reason = f"已切换到授权 {target_id}，原主授权待修复：{reason}"


def _promote_authorization(account: TgAccount, target: TgAccountAuthorization) -> None:
    target.role = PRIMARY_ROLE
    target.status = "active"
    target.health_status = "healthy"
    target.is_current = True
    target.last_switched_at = _now()
    target.last_success_at = _now()
    account.session_ciphertext = target.session_ciphertext
    account.developer_app_id = target.developer_app_id
    account.proxy_id = target.proxy_id
    account.status = AccountStatus.ACTIVE.value
    account.last_active_at = _now()
    account.health_score = max(account.health_score, 90)
    app = session_app_version(target)
    if app is not None:
        account.developer_app_version = app.credentials_version


def session_app_version(target: TgAccountAuthorization) -> TelegramDeveloperApp | None:
    return target.developer_app if target.developer_app_id else None
