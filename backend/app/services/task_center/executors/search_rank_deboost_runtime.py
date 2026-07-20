from __future__ import annotations

from collections.abc import Callable
import hashlib
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, TgAccount
from app.models.search_rank_deboost import SearchRankDeboostActionStat
from app.security import decrypt_secret
from app.services._common import _now
from app.services.search_rank_deboost_alerts import (
    record_all_exempt_clicks_alert,
    record_join_button_violation_alert,
)
from app.services.task_center.payloads import SearchRankDeboostPayload
from app.services.task_center.rank_deboost_runtime_authorization import (
    RankDeboostRuntimeAuthorization,
    resolve_rank_deboost_runtime_authorization,
)
from app.services.task_center.search_rank_deboost_reservations import (
    consume_reservation,
    mark_reservation_unknown,
    release_reserved_reservation,
    release_reservation,
)

from ..search_rank_deboost import (
    ALL_EXEMPT_CLICKS,
    RANK_OBSERVATION_GATEWAY_UNAVAILABLE,
    TARGET_NOT_IN_RESULTS,
)
from .search_rank_deboost_runtime_alerts import record_proxy_egress_alert


PROXY_EGRESS_GUARD_FAILED = "proxy_egress_guard_failed"
GATEWAY_CONTRACT_INVALID = "rank_deboost_gateway_contract_invalid"
NO_CLICK_STATUSES = {
    ALL_EXEMPT_CLICKS,
    "no_navigable_button",
    "observed_no_click",
    TARGET_NOT_IN_RESULTS,
    "target_identity_missing",
}
_NO_EXPLICIT_PROBE = object()


def execute_search_rank_deboost(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    *,
    gateway_execute: Any | None = None,
    probe_exit_ip: Any = _NO_EXPLICIT_PROBE,
    before_gateway_call: Callable[[], None] | None = None,
) -> dict:
    binding_id = _binding_id(payload)
    if binding_id <= 0:
        return _failure_without_click(session, action, PROXY_EGRESS_GUARD_FAILED, "排名观察任务缺少 group_proxy_binding_id")
    if probe_exit_ip is not _NO_EXPLICIT_PROBE and not _verify_proxy_egress(session, action, account, binding_id, probe_exit_ip):
        return _failure_without_click(session, action, PROXY_EGRESS_GUARD_FAILED, "分组级代理出口校验失败，禁止回退本机直连")
    try:
        authorization = resolve_rank_deboost_runtime_authorization(session, account, payload)
    except ValueError as exc:
        return _failure_without_click(session, action, str(exc), "排名观察授权上下文不可用")
    gateway_called, gateway_result = _invoke_gateway(
        gateway_execute,
        account,
        payload,
        authorization,
        before_gateway_call=before_gateway_call,
    )
    if not gateway_called:
        return _failure_without_click(session, action, GATEWAY_CONTRACT_INVALID, "搜索排名观察 Gateway 不可用")
    if not isinstance(gateway_result, dict):
        return _failure_after_gateway(session, action, GATEWAY_CONTRACT_INVALID, "搜索排名观察 Gateway 未返回对象结果")
    if not _gateway_egress_verified(session, action, account, binding_id, gateway_result, probe_exit_ip):
        return _failure_after_gateway(session, action, PROXY_EGRESS_GUARD_FAILED, "分组级代理出口校验失败，禁止回退本机直连")
    return _handle_gateway_result(session, action, account, payload, gateway_result)


def _binding_id(payload: SearchRankDeboostPayload) -> int:
    runtime = payload.runtime_environment or {}
    return _as_int(runtime.get("group_proxy_binding_id"))


def _gateway_egress_verified(
    session: Session,
    action: Action,
    account: TgAccount,
    binding_id: int,
    result: dict,
    probe_exit_ip: Any,
) -> bool:
    observed_exit_ip = _observed_exit_ip(result)
    if observed_exit_ip:
        return _verify_proxy_egress(session, action, account, binding_id, observed_exit_ip)
    if probe_exit_ip is not _NO_EXPLICIT_PROBE:
        return True
    if not str(result.get("execution_status") or "").strip():
        return True
    return _verify_proxy_egress(session, action, account, binding_id, None)


def _verify_proxy_egress(
    session: Session,
    action: Action,
    account: TgAccount,
    binding_id: int,
    probe_exit_ip: str | None,
) -> bool:
    from app.services.proxy_group_binding_service import verify_group_proxy_egress

    if verify_group_proxy_egress(session, binding_id=binding_id, probe_exit_ip=probe_exit_ip):
        return True
    record_proxy_egress_alert(session, action=action, account=account, binding_id=binding_id, probe_exit_ip=probe_exit_ip)
    return False


def _invoke_gateway(
    gateway_execute: Any | None,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    authorization: RankDeboostRuntimeAuthorization,
    *,
    before_gateway_call: Callable[[], None] | None,
) -> tuple[bool, Any]:
    if gateway_execute is None:
        from app.services._common import gateway

        gateway_execute = getattr(gateway, "execute_search_rank_deboost", None)
    if not callable(gateway_execute):
        return False, None
    keyword_text = decrypt_secret(payload.keyword_text_ciphertext) or ""
    if before_gateway_call is not None:
        before_gateway_call()
    return True, gateway_execute(
        account.id,
        payload.model_dump(mode="json"),
        session_ciphertext=authorization.session_ciphertext,
        credentials=authorization.credentials,
        keyword_text=keyword_text,
    )


def _handle_gateway_result(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    result: dict,
) -> dict:
    if _gateway_reported_no_call(result):
        return _failure_without_click(session, action, _gateway_error_code(result), _gateway_error_message(result))
    status = str(result.get("execution_status") or "").strip()
    if status == "confirmed":
        return _handle_confirmed_result(session, action, account, payload, result)
    if status == "unknown_after_click":
        mark_reservation_unknown(session, action.id)
        return {**result, "success": False}
    if status in NO_CLICK_STATUSES:
        if status == ALL_EXEMPT_CLICKS:
            _record_all_exempt_alert(session, action)
        release_reservation(session, action.id)
        return {**result, "success": False, "skip_reason": status}
    return _failure_after_gateway(session, action, _gateway_error_code(result), _gateway_error_message(result))


def _handle_confirmed_result(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    result: dict,
) -> dict:
    outcome = _confirmed_outcome(result)
    if outcome is None:
        return _failure_after_gateway(session, action, GATEWAY_CONTRACT_INVALID, "confirmed 结果缺少一条完整的实际点击事实")
    if not _outcome_is_safe(outcome):
        return _handle_unsafe_outcome(
            session,
            action,
            account,
            payload=payload,
            outcome=outcome,
            observed_exit_ip=_observed_exit_ip(result),
        )
    consume_reservation(session, action.id)
    _write_factual_stat(
        session,
        action,
        payload,
        outcome=outcome,
        observed_exit_ip=_observed_exit_ip(result),
    )
    return {**result, "success": True}


def _confirmed_outcome(result: dict) -> dict | None:
    outcomes = result.get("click_outcomes")
    if not isinstance(outcomes, list) or len(outcomes) != 1:
        return None
    outcome = outcomes[0]
    return outcome if isinstance(outcome, dict) and outcome.get("status") == "confirmed" and _outcome_complete(outcome) else None


def _outcome_complete(outcome: dict) -> bool:
    identity = str(outcome.get("competitor_username") or outcome.get("competitor_peer_id") or "").strip()
    required = ("competitor_position", "row", "col", "dwell_seconds", "effect", "joined")
    return bool(
        identity
        and all(key in outcome for key in required)
        and _as_int(outcome.get("competitor_position")) > 0
        and str(outcome.get("effect") or "").strip()
        and isinstance(outcome.get("joined"), bool)
    )


def _outcome_is_safe(outcome: dict) -> bool:
    return str(outcome.get("effect") or "") == "navigate_only" and outcome.get("joined") is False


def _handle_unsafe_outcome(
    session: Session,
    action: Action,
    account: TgAccount,
    *,
    payload: SearchRankDeboostPayload,
    outcome: dict,
    observed_exit_ip: str,
) -> dict:
    mark_reservation_unknown(session, action.id)
    account.status = AccountStatus.LIMITED.value
    _write_factual_stat(
        session,
        action,
        payload,
        outcome=outcome,
        observed_exit_ip=observed_exit_ip,
        joined=bool(outcome.get("joined")),
        join_button_detected=True,
        join_button_violation=True,
    )
    session.flush()
    record_join_button_violation_alert(
        session,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(account.id),
        competitor_username=str(outcome.get("competitor_username") or ""),
        button_effect=str(outcome.get("effect") or ""),
    )
    return {
        "success": False,
        "error_code": "join_button_violation",
        "error_message": "Gateway 返回了禁止的点击效果",
        "join_button_violation": True,
    }


def _write_factual_stat(
    session: Session,
    action: Action,
    payload: SearchRankDeboostPayload,
    *,
    outcome: dict,
    observed_exit_ip: str,
    joined: bool = False,
    join_button_detected: bool = False,
    join_button_violation: bool = False,
) -> None:
    session.add(SearchRankDeboostActionStat(
        id=str(uuid4()),
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(action.account_id or 0),
        account_pool_id=int(payload.account_pool_id),
        proxy_airport_node_id=int(payload.proxy_airport_node_id) or None,
        observed_exit_ip=observed_exit_ip,
        bot_username=payload.bot_username,
        keyword_hash=payload.keyword_hash,
        competitor_group_username=str(outcome.get("competitor_username") or ""),
        competitor_group_peer_id=str(outcome.get("competitor_peer_id") or ""),
        competitor_group_title=str(outcome.get("competitor_title") or ""),
        competitor_position=_as_int(outcome.get("competitor_position")),
        button_hash=_button_hash(outcome),
        button_effect=str(outcome.get("effect") or ""),
        join_button_detected=join_button_detected,
        joined=joined,
        join_button_violation=join_button_violation,
        dwell_seconds=_as_int(outcome.get("dwell_seconds")),
        hour_bucket=_now().replace(minute=0, second=0, microsecond=0),
        captured_at=_now(),
        skip_reason="",
    ))
    session.flush()


def _button_hash(outcome: dict) -> str:
    raw = f"{outcome.get('text', '')}:{outcome.get('url', '')}:{_as_int(outcome.get('competitor_position'))}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _record_all_exempt_alert(session: Session, action: Action) -> None:
    record_all_exempt_clicks_alert(
        session,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(action.account_id or 0) or None,
    )


def _gateway_error_code(result: dict) -> str:
    return str(result.get("error_code") or result.get("execution_status") or GATEWAY_CONTRACT_INVALID)


def _gateway_reported_no_call(result: dict) -> bool:
    return str(result.get("error_code") or "") == RANK_OBSERVATION_GATEWAY_UNAVAILABLE


def _gateway_error_message(result: dict) -> str:
    return str(result.get("error_message") or result.get("detail") or "搜索排名观察 Gateway 未返回可验证事实")


def _observed_exit_ip(result: dict) -> str:
    return str(result.get("observed_exit_ip") or result.get("probe_exit_ip") or "").strip()


def _failure_without_click(session: Session, action: Action, code: str, detail: str) -> dict:
    release_reserved_reservation(session, action.id)
    return {"success": False, "error_code": code, "error_message": detail, "validation_stage": "search_rank_deboost"}


def _failure_after_gateway(session: Session, action: Action, code: str, detail: str) -> dict:
    mark_reservation_unknown(session, action.id)
    return {
        "success": False,
        "execution_status": "unknown_after_click",
        "error_code": code,
        "error_message": detail,
        "validation_stage": "search_rank_deboost",
    }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


__all__ = ["execute_search_rank_deboost"]
