from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.config import get_settings
from app.models import AccountStatus, TgAccount, TgLoginFlow
from app.security import decrypt_secret, encrypt_secret, encrypt_session
from app.services._common import _now, gateway, get_account_phone
from app.services.accounts import login_error_from_exception
from app.services.code_source_client import CodeSourceClient
from app.services.developer_apps import credentials_for_account

from .contracts import BatchLoginError, LoginMaterials
from .host_rate_policy import item_host_rate_policy
from .identity import material_hmac
from .post_initialization import (
    attach_authorized_full_initialization,
    complete_online_readback,
    fail_full_initialization_online_readback,
    requires_full_initialization,
)
from .rate_limit import RateLease, acquire_rate_lease, release_rate_lease
from .state import (
    PhaseClaim,
    advance_claim,
    commit_claim,
    fail_claim,
    load_claim,
    mark_claim_unknown,
)


@dataclass(frozen=True)
class LoginCallInputs:
    account_id: int
    flow_id: int
    phone: str
    credentials: object
    temporary_session: str
    phone_code_hash: str


def execute_remote_phase(session_factory, claim: PhaseClaim, code_client: CodeSourceClient) -> None:
    handlers = {
        "authorization_probe": _authorization_probe,
        "code_baseline": _code_baseline,
        "send_code": _send_code,
        "wait_code": _wait_code,
        "online_readback": _online_readback,
    }
    handler = handlers.get(claim.phase)
    if not handler:
        raise RuntimeError(f"unsupported remote batch login phase: {claim.phase}")
    handler(session_factory, claim, code_client)


def _authorization_probe(session_factory, claim: PhaseClaim, _client: CodeSourceClient) -> None:
    with session_factory() as session:
        item, _ = load_claim(session, claim)
        account = _item_account(session, item)
        credentials = credentials_for_account(session, account)
        session_ciphertext = account.session_ciphertext
    try:
        health = gateway.check_account_health_isolated(session_ciphertext, credentials)
    except Exception as exc:
        _fail_external(session_factory, claim, "authorization_probe_failed", _safe_error("授权探测失败", exc))
        return
    with session_factory() as session:
        item, attempt = load_claim(session, claim)
        account = _item_account(session, item)
        if health.status == AccountStatus.ACTIVE.value:
            item.route = "already_authorized"
            account.status = AccountStatus.ACTIVE.value
            advance_claim(session, claim, "bind_code_source")
        else:
            item.route = "relogin"
            attempt.deadline_at = None
            advance_claim(session, claim, "code_baseline")
        commit_claim(session)


def _code_baseline(session_factory, claim: PhaseClaim, client: CodeSourceClient) -> None:
    lease = _acquire_host_lease(session_factory, claim)
    if not lease:
        return
    try:
        _start_claim_deadline(session_factory, claim)
        try:
            url = _code_url(session_factory, claim)
            materials = client.fetch_login_materials(url)
        except BatchLoginError as exc:
            _fail_external(session_factory, claim, exc.code, str(exc))
            return
        except Exception as exc:
            _fail_external(session_factory, claim, "url_fetch_failed", _safe_error("接码基线读取失败", exc))
            return
        with session_factory() as session:
            item, attempt = load_claim(session, claim)
            attempt.baseline_code_hmac = material_hmac(materials.code)
            attempt.baseline_login_time_hmac = material_hmac(materials.login_time)
            next_phase = "bind_account" if not item.account_id else "bind_code_source"
            advance_claim(session, claim, next_phase)
            commit_claim(session)
    finally:
        _release_lease(session_factory, lease)


def _start_claim_deadline(session_factory, claim: PhaseClaim) -> None:
    with session_factory() as session:
        _, attempt = load_claim(session, claim)
        if attempt.deadline_at:
            return
        attempt.deadline_at = _now() + timedelta(
            seconds=get_settings().account_batch_login_item_deadline_seconds,
        )
        attempt.state_version += 1
        session.commit()


def _send_code(session_factory, claim: PhaseClaim, _client: CodeSourceClient) -> None:
    try:
        inputs, app_id = _login_inputs(session_factory, claim)
    except Exception as exc:
        _fail_external(session_factory, claim, "developer_app_unavailable", _safe_error("登录凭据不可用", exc))
        return
    lease = _acquire_developer_app_lease(session_factory, claim, app_id)
    if not lease:
        return
    try:
        _mark_call_started(session_factory, claim, "send")
        try:
            challenge = gateway.start_login(
                "code",
                flow_id=inputs.flow_id,
                account_id=inputs.account_id,
                phone=inputs.phone,
                credentials=inputs.credentials,
            )
        except Exception as exc:
            _handle_login_call_error(session_factory, claim, exc, "send_call_state")
            return
        _persist_challenge(session_factory, claim, challenge)
    finally:
        _release_lease(session_factory, lease)


def _wait_code(session_factory, claim: PhaseClaim, client: CodeSourceClient) -> None:
    lease = _acquire_host_lease(session_factory, claim)
    if not lease:
        return
    try:
        try:
            materials = client.fetch_login_materials(_code_url(session_factory, claim))
        except BatchLoginError as exc:
            _fail_external(session_factory, claim, exc.code, str(exc))
            return
        except Exception as exc:
            _fail_external(session_factory, claim, "url_fetch_failed", _safe_error("接码轮询失败", exc))
            return
        if not _materials_changed(session_factory, claim, materials):
            _wait_or_timeout(session_factory, claim)
            return
        _verify_materials(session_factory, claim, materials)
    finally:
        _release_lease(session_factory, lease)


def _online_readback(session_factory, claim: PhaseClaim, _client: CodeSourceClient) -> None:
    with session_factory() as session:
        item, _ = load_claim(session, claim)
        account = _item_account(session, item)
        credentials = credentials_for_account(session, account)
        session_ciphertext = account.session_ciphertext
        full_init_required = requires_full_initialization(item)
    try:
        health = gateway.check_account_health_isolated(session_ciphertext, credentials)
    except Exception as exc:
        if full_init_required:
            fail_full_initialization_online_readback(
                session_factory,
                claim,
                _safe_error("A 在线读回失败", exc),
            )
            return
        warning = "授权已持久化，但在线回读失败"
    else:
        if full_init_required and health.status != AccountStatus.ACTIVE.value:
            fail_full_initialization_online_readback(
                session_factory,
                claim,
                health.detail or health.status,
            )
            return
        warning = "" if health.status == AccountStatus.ACTIVE.value else "授权已持久化，但在线回读未确认"
    complete_online_readback(session_factory, claim, warning)


def _login_inputs(session_factory, claim: PhaseClaim) -> tuple[LoginCallInputs, int]:
    with session_factory() as session:
        item, attempt = load_claim(session, claim)
        account = _item_account(session, item)
        flow = _item_flow(session, item, attempt)
        credentials = credentials_for_account(session, account, assign_if_missing=True)
        session.commit()
        return LoginCallInputs(
            account_id=account.id,
            flow_id=flow.id,
            phone=get_account_phone(account) or "",
            credentials=credentials,
            temporary_session=decrypt_secret(flow.temporary_session_ciphertext) or "",
            phone_code_hash=decrypt_secret(flow.phone_code_hash_ciphertext) or "",
        ), int(account.developer_app_id or 0)


def _mark_call_started(session_factory, claim: PhaseClaim, call_name: str) -> None:
    with session_factory() as session:
        _, attempt = load_claim(session, claim)
        sequence_field = f"{call_name}_request_seq"
        state_field = f"{call_name}_call_state"
        key_field = f"{call_name}_request_key"
        sequence = int(getattr(attempt, sequence_field)) + 1
        setattr(attempt, sequence_field, sequence)
        setattr(attempt, key_field, f"{attempt.id}:{claim.generation}:{call_name}:{sequence}")
        setattr(attempt, state_field, "started")
        attempt.state_version += 1
        session.commit()


def _persist_challenge(session_factory, claim: PhaseClaim, challenge) -> None:
    with session_factory() as session:
        item, attempt = load_claim(session, claim)
        account = _item_account(session, item)
        flow = _item_flow(session, item, attempt)
        now = _now()
        account.status = challenge.status
        flow.status = challenge.status
        flow.challenge_sent_at = now
        flow.code_expires_at = challenge.code_expires_at
        flow.temporary_session_ciphertext = encrypt_secret(challenge.temporary_session) if challenge.temporary_session else None
        flow.phone_code_hash_ciphertext = encrypt_secret(challenge.phone_code_hash) if challenge.phone_code_hash else None
        attempt.send_call_state = "confirmed"
        wait_until = now + timedelta(seconds=get_settings().account_batch_login_code_wait_seconds)
        attempt.code_wait_until_at = wait_until
        advance_claim(session, claim, "wait_code", status="waiting", next_retry_at=now)
        commit_claim(session)


def _verify_materials(session_factory, claim: PhaseClaim, materials: LoginMaterials) -> None:
    inputs, _ = _login_inputs(session_factory, claim)
    _mark_call_started(session_factory, claim, "code_verify")
    try:
        status, raw_session = gateway.finish_login(
            materials.code,
            None,
            flow_id=inputs.flow_id,
            account_id=inputs.account_id,
            phone=inputs.phone,
            credentials=inputs.credentials,
            temporary_session=inputs.temporary_session,
            phone_code_hash=inputs.phone_code_hash,
        )
    except Exception as exc:
        _handle_login_call_error(session_factory, claim, exc, "code_verify_call_state")
        return
    if status == AccountStatus.WAITING_2FA.value:
        _verify_two_fa(session_factory, claim, inputs, raw_session, materials.password_2fa)
        return
    if status != AccountStatus.ACTIVE.value or not raw_session:
        _fail_confirmed_call(session_factory, claim, "code_verify_call_state", "login_remote_not_completed", "Telegram 未返回已授权 session")
        return
    _persist_authorized_session(
        session_factory,
        claim,
        raw_session,
        state_field="code_verify_call_state",
        source_two_fa_kind="telegram_missing",
    )


def _verify_two_fa(
    session_factory,
    claim: PhaseClaim,
    inputs: LoginCallInputs,
    temporary_session: str,
    password_2fa: str,
) -> None:
    if not password_2fa:
        _fail_external(session_factory, claim, "url_missing_2fa", "接码页面缺少二步密码")
        return
    _persist_waiting_two_fa(session_factory, claim, temporary_session)
    _mark_call_started(session_factory, claim, "twofa_verify")
    try:
        status, raw_session = gateway.finish_login(
            None,
            password_2fa,
            flow_id=inputs.flow_id,
            account_id=inputs.account_id,
            phone=inputs.phone,
            credentials=inputs.credentials,
            temporary_session=temporary_session,
            phone_code_hash=inputs.phone_code_hash,
        )
    except Exception as exc:
        _handle_login_call_error(session_factory, claim, exc, "twofa_verify_call_state")
        return
    if status != AccountStatus.ACTIVE.value or not raw_session:
        _fail_confirmed_call(session_factory, claim, "twofa_verify_call_state", "login_remote_not_completed", "Telegram 二步验证未完成")
        return
    _persist_authorized_session(
        session_factory,
        claim,
        raw_session,
        state_field="twofa_verify_call_state",
        source_two_fa_kind="telegram_accepted",
        source_two_fa_password=password_2fa,
    )


def _persist_waiting_two_fa(session_factory, claim: PhaseClaim, raw_session: str) -> None:
    with session_factory() as session:
        item, attempt = load_claim(session, claim)
        flow = _item_flow(session, item, attempt)
        flow.status = AccountStatus.WAITING_2FA.value
        flow.temporary_session_ciphertext = encrypt_secret(raw_session)
        attempt.code_verify_call_state = "confirmed"
        session.commit()


def _persist_authorized_session(
    session_factory,
    claim: PhaseClaim,
    raw_session: str,
    *,
    state_field: str,
    source_two_fa_kind: str,
    source_two_fa_password: str = "",
) -> None:
    with session_factory() as session:
        item, attempt = load_claim(session, claim)
        account = _item_account(session, item)
        flow = _item_flow(session, item, attempt)
        account.session_ciphertext = encrypt_session(raw_session)
        account.status = AccountStatus.ACTIVE.value
        account.last_active_at = _now()
        account.health_score = max(account.health_score, 90)
        flow.status = AccountStatus.ACTIVE.value
        flow.code_preview = None
        flow.temporary_session_ciphertext = None
        flow.phone_code_hash_ciphertext = None
        setattr(attempt, state_field, "confirmed")
        attach_authorized_full_initialization(
            session,
            item,
            source_two_fa_kind=source_two_fa_kind,
            source_two_fa_password=source_two_fa_password,
        )
        advance_claim(session, claim, "pool_transition")
        commit_claim(session)


def _materials_changed(session_factory, claim: PhaseClaim, materials: LoginMaterials) -> bool:
    with session_factory() as session:
        _, attempt = load_claim(session, claim)
        if not materials.code:
            return False
        code_changed = material_hmac(materials.code) != attempt.baseline_code_hmac
        time_changed = material_hmac(materials.login_time) != attempt.baseline_login_time_hmac
        return code_changed or time_changed


def _wait_or_timeout(session_factory, claim: PhaseClaim) -> None:
    with session_factory() as session:
        _, attempt = load_claim(session, claim)
        now = _now()
        code_wait_due = attempt.code_wait_until_at and now >= attempt.code_wait_until_at
        deadline_due = attempt.deadline_at and now >= attempt.deadline_at
        if code_wait_due and (not deadline_due or attempt.code_wait_until_at <= attempt.deadline_at):
            fail_claim(session, claim, "code_timeout", "等待新验证码超过 120 秒")
        elif deadline_due:
            fail_claim(session, claim, "item_deadline_exceeded", "单行登录等待超过 300 秒")
        else:
            retry_at = now + timedelta(seconds=get_settings().account_batch_login_poll_interval_seconds)
            advance_claim(session, claim, "wait_code", status="waiting", next_retry_at=retry_at)
        commit_claim(session)


def _handle_login_call_error(session_factory, claim: PhaseClaim, exc: Exception, state_field: str) -> None:
    code, message, _ = login_error_from_exception(exc)
    code = "twofa_invalid" if code == "login_2fa_invalid" else code
    if code == "login_remote_unknown":
        with session_factory() as session:
            mark_claim_unknown(session, claim, state_field)
            commit_claim(session)
        return
    if code == "login_rate_limited":
        _schedule_rate_limit(session_factory, claim, exc, state_field, message)
        return
    _fail_confirmed_call(session_factory, claim, state_field, code, message)


def _schedule_rate_limit(session_factory, claim: PhaseClaim, exc: Exception, state_field: str, message: str) -> None:
    wait_seconds = max(int(getattr(exc, "seconds", 0) or 1), 1)
    with session_factory() as session:
        _, attempt = load_claim(session, claim)
        setattr(attempt, state_field, "confirmed")
        retry_at = _now() + timedelta(seconds=wait_seconds)
        if attempt.deadline_at and retry_at >= attempt.deadline_at:
            fail_claim(session, claim, "login_rate_limited", message)
        else:
            advance_claim(session, claim, claim.phase, status="waiting", next_retry_at=retry_at)
        commit_claim(session)


def _fail_confirmed_call(session_factory, claim: PhaseClaim, state_field: str, code: str, detail: str) -> None:
    with session_factory() as session:
        _, attempt = load_claim(session, claim)
        setattr(attempt, state_field, "confirmed")
        fail_claim(session, claim, code, detail)
        commit_claim(session)


def _acquire_host_lease(session_factory, claim: PhaseClaim) -> RateLease | None:
    settings = get_settings()
    scope_id, min_interval = item_host_rate_policy(session_factory, claim, settings.account_batch_login_host_min_interval_seconds)
    return _acquire_lease(
        session_factory,
        claim,
        "host",
        scope_id,
        settings.account_batch_login_host_concurrency,
        min_interval,
    )


def _acquire_developer_app_lease(session_factory, claim: PhaseClaim, app_id: int) -> RateLease | None:
    settings = get_settings()
    return _acquire_lease(
        session_factory,
        claim,
        "developer_app",
        str(app_id),
        settings.account_batch_login_developer_app_concurrency,
        0,
    )


def _acquire_lease(
    session_factory,
    claim: PhaseClaim,
    scope_type: str,
    scope_id: str,
    max_concurrency: int,
    min_interval: float,
) -> RateLease | None:
    with session_factory() as session:
        result = acquire_rate_lease(
            session,
            scope_type=scope_type,
            scope_id=scope_id,
            max_concurrency=max_concurrency,
            min_interval_seconds=min_interval,
        )
    if result.lease:
        return result.lease
    with session_factory() as session:
        advance_claim(session, claim, claim.phase, status="waiting", next_retry_at=result.retry_at)
        commit_claim(session)
    return None


def _release_lease(session_factory, lease: RateLease) -> None:
    with session_factory() as session:
        release_rate_lease(session, lease)


def _code_url(session_factory, claim: PhaseClaim) -> str:
    with session_factory() as session:
        item, _ = load_claim(session, claim)
        if not item.credential_expires_at or item.credential_expires_at <= _now():
            raise BatchLoginError("credential_expired", "接码地址已过期")
        url = decrypt_secret(item.code_url_ciphertext)
        if not url:
            raise BatchLoginError("credential_expired", "接码地址已清除")
        return url


def _fail_external(session_factory, claim: PhaseClaim, code: str, detail: str) -> None:
    with session_factory() as session:
        fail_claim(session, claim, code, detail)
        commit_claim(session)


def _item_account(session, item) -> TgAccount:
    account = session.get(TgAccount, item.account_id)
    if not account or account.tenant_id != item.tenant_id or account.deleted_at is not None:
        raise BatchLoginError("account_create_failed", "批量行账号不存在")
    return account


def _item_flow(session, item, attempt) -> TgLoginFlow:
    flow = session.get(TgLoginFlow, attempt.flow_id)
    if not flow or flow.account_id != item.account_id or flow.batch_login_attempt_id != attempt.id:
        raise BatchLoginError("login_flow_conflict", "批量登录 flow 归属不匹配")
    if flow.flow_version != attempt.flow_version or flow.batch_login_generation != item.execution_generation:
        raise BatchLoginError("login_flow_conflict", "批量登录 flow 版本已变化")
    return flow


def _safe_error(message: str, exc: Exception) -> str:
    return f"{message}: {exc.__class__.__name__}"[:300]


__all__ = ["execute_remote_phase"]
