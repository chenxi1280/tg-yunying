from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import AccountStatus, Action, TgAccount
from app.models.search_rank_deboost import AccountGroupProxyBinding, SearchRankDeboostActionStat
from app.security import decrypt_secret
from app.services._common import _now
from app.services.search_rank_deboost_alerts import (
    record_all_exempt_clicks_alert,
    record_group_proxy_egress_failure_alert,
    record_join_button_violation_alert,
)
from app.services.task_center.payloads import SearchRankDeboostPayload

from ..search_rank_deboost import (
    ALL_EXEMPT_CLICKS,
    RANK_OBSERVATION_GATEWAY_UNAVAILABLE,
    TARGET_NOT_IN_RESULTS,
    compute_deboost_click_targets,
)
from ..search_rank_deboost_pacing import DEFAULT_DWELL_SECONDS_MAX, DEFAULT_DWELL_SECONDS_MIN


PROXY_EGRESS_GUARD_FAILED = "proxy_egress_guard_failed"
NO_NAVIGABLE_BUTTON = "no_navigable_button"
JOIN_BUTTON_VIOLATION = "join_button_violation"
GATEWAY_UNAVAILABLE = RANK_OBSERVATION_GATEWAY_UNAVAILABLE

NAVIGABLE_BUTTON_EFFECTS = {"navigate_only"}
JOIN_CANDIDATE_EFFECTS = {"join_candidate"}
_NO_EXPLICIT_PROBE = object()


@dataclass(frozen=True)
class ClickRunResult:
    clicked_count: int
    violation_detected: bool


def execute_search_rank_deboost(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    *,
    gateway_execute: Any | None = None,
    probe_exit_ip: Any = _NO_EXPLICIT_PROBE,
) -> dict:
    """执行单条搜索排名观察 action；真实 gateway 不可用时显式 skip。"""
    binding_id = _binding_id(payload)
    if binding_id <= 0:
        return _skip_result(action, PROXY_EGRESS_GUARD_FAILED, "排名观察任务缺少 group_proxy_binding_id")

    if probe_exit_ip is not _NO_EXPLICIT_PROBE:
        if not _verify_proxy_egress(session, action, account, binding_id, probe_exit_ip):
            return _skip_result(action, PROXY_EGRESS_GUARD_FAILED, "分组级代理出口校验失败，禁止回退本机直连")

    gateway_result = _invoke_gateway(gateway_execute, account, payload)
    if gateway_result is None:
        return _skip_result(action, GATEWAY_UNAVAILABLE, "搜索排名观察 gateway 尚未接入真实 MTProto 执行器")

    runtime_probe_ip = _runtime_probe_exit_ip(gateway_result)
    if runtime_probe_ip is None and probe_exit_ip is _NO_EXPLICIT_PROBE:
        if not _verify_proxy_egress(session, action, account, binding_id, None):
            return _skip_result(action, PROXY_EGRESS_GUARD_FAILED, "分组级代理出口校验失败，禁止回退本机直连")
    if runtime_probe_ip is not None and not _verify_proxy_egress(session, action, account, binding_id, runtime_probe_ip):
        return _skip_result(action, PROXY_EGRESS_GUARD_FAILED, "分组级代理出口校验失败，禁止回退本机直连")

    decision_or_skip = _search_decision(session, action, payload, gateway_result)
    if "skip_reason" in decision_or_skip:
        return decision_or_skip

    click_targets = decision_or_skip.get("click_targets") or []
    run = _process_click_targets(session, action, account, payload, click_targets)
    return _success_result(decision_or_skip, click_targets, run)


def _binding_id(payload: SearchRankDeboostPayload) -> int:
    runtime = payload.runtime_environment or {}
    return int(runtime.get("group_proxy_binding_id") or 0)


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
    _record_proxy_egress_alert(session, action=action, account=account, binding_id=binding_id, probe_exit_ip=probe_exit_ip)
    return False


def _search_decision(
    session: Session,
    action: Action,
    payload: SearchRankDeboostPayload,
    gateway_result: dict,
) -> dict:
    search_results = _gateway_search_results(gateway_result)
    decision = compute_deboost_click_targets(
        search_results=search_results,
        my_target_ids=list(payload.target_group_ids),
        exempt_group_username=payload.exempt_group_username,
    )
    return _decision_or_skip(session, action, decision)


def _decision_or_skip(session: Session, action: Action, decision: dict) -> dict:
    skipped_reason = decision.get("skipped_reason")
    if skipped_reason == TARGET_NOT_IN_RESULTS:
        return _skip_result(action, TARGET_NOT_IN_RESULTS, "我方目标群未出现在搜索结果")
    if skipped_reason != ALL_EXEMPT_CLICKS:
        return decision
    record_all_exempt_clicks_alert(
        session,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(action.account_id or 0) or None,
    )
    return _skip_result(action, ALL_EXEMPT_CLICKS, "所有结果都被白名单豁免")


def _process_click_targets(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    click_targets: list[dict],
) -> ClickRunResult:
    hour_bucket = _now().replace(minute=0, second=0, microsecond=0)
    clicked_count = 0
    for target in click_targets:
        outcome = _process_click_target(session, action, account, payload, target, hour_bucket)
        clicked_count += int(outcome == "clicked")
        if outcome == JOIN_BUTTON_VIOLATION:
            return ClickRunResult(clicked_count=clicked_count, violation_detected=True)
    return ClickRunResult(clicked_count=clicked_count, violation_detected=False)


def _process_click_target(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    target: dict,
    hour_bucket: datetime,
) -> str:
    buttons = _extract_buttons(target)
    join_button_detected = any(btn.get("effect") in JOIN_CANDIDATE_EFFECTS for btn in buttons)
    navigable = [btn for btn in buttons if btn.get("effect") in NAVIGABLE_BUTTON_EFFECTS]
    if not navigable:
        _write_no_navigable_stat(session, action, payload, target, join_button_detected, hour_bucket)
        return NO_NAVIGABLE_BUTTON
    return _handle_navigable_button(
        session, action, account, payload, target, navigable[0], join_button_detected, hour_bucket
    )


def _handle_navigable_button(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    target: dict,
    button: dict,
    join_button_detected: bool,
    hour_bucket: datetime,
) -> str:
    if button.get("effect") in JOIN_CANDIDATE_EFFECTS:
        _record_join_violation(session, action, account, payload, target, button, join_button_detected, hour_bucket)
        return JOIN_BUTTON_VIOLATION
    _write_clicked_stat(session, action, payload, target, button, join_button_detected, hour_bucket)
    return "clicked"


def _write_no_navigable_stat(
    session: Session,
    action: Action,
    payload: SearchRankDeboostPayload,
    target: dict,
    join_button_detected: bool,
    hour_bucket: datetime,
) -> None:
    _write_stat(
        session,
        action=action,
        payload=payload,
        target=target,
        button=None,
        dwell_seconds=0,
        join_button_detected=join_button_detected,
        joined=False,
        skip_reason=NO_NAVIGABLE_BUTTON,
        hour_bucket=hour_bucket,
    )


def _record_join_violation(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SearchRankDeboostPayload,
    target: dict,
    button: dict,
    join_button_detected: bool,
    hour_bucket: datetime,
) -> None:
    _write_stat(
        session,
        action=action,
        payload=payload,
        target=target,
        button=button,
        dwell_seconds=0,
        join_button_detected=True,
        joined=True,
        skip_reason=JOIN_BUTTON_VIOLATION,
        hour_bucket=hour_bucket,
        join_button_violation=True,
    )
    account.status = AccountStatus.LIMITED.value
    session.flush()
    record_join_button_violation_alert(
        session,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(account.id),
        competitor_username=str(target.get("username") or ""),
        button_effect=str(button.get("effect") or ""),
    )


def _write_clicked_stat(
    session: Session,
    action: Action,
    payload: SearchRankDeboostPayload,
    target: dict,
    button: dict,
    join_button_detected: bool,
    hour_bucket: datetime,
) -> None:
    _write_stat(
        session,
        action=action,
        payload=payload,
        target=target,
        button=button,
        dwell_seconds=_random_dwell(payload),
        join_button_detected=join_button_detected,
        joined=False,
        skip_reason="",
        hour_bucket=hour_bucket,
    )


def _success_result(decision: dict, click_targets: list[dict], run: ClickRunResult) -> dict:
    result = {
        "success": not run.violation_detected,
        "click_targets_count": len(click_targets),
        "clicked_count": run.clicked_count,
        "my_target_position": decision.get("my_target_position"),
        "exempt_position": decision.get("exempt_position"),
        "join_button_violation": run.violation_detected,
    }
    if run.violation_detected:
        result["error_code"] = JOIN_BUTTON_VIOLATION
        result["error_message"] = "误点加入按钮，已停止 action 并暂停账号"
    return result


def _invoke_gateway(gateway_execute: Any | None, account: TgAccount, payload: SearchRankDeboostPayload) -> dict | None:
    if gateway_execute is None:
        from app.services._common import gateway

        gateway_execute = getattr(gateway, "execute_search_rank_deboost", None)
    if not callable(gateway_execute):
        return None
    keyword_text = decrypt_secret(payload.keyword_text_ciphertext) or ""
    result = gateway_execute(account.id, payload.model_dump(mode="json"), keyword_text)
    if not isinstance(result, dict) or not result.get("success"):
        return None
    return result


def _gateway_search_results(result: dict) -> list[dict]:
    search_results = result.get("search_results") or result.get("results") or []
    return search_results if isinstance(search_results, list) else []


def _runtime_probe_exit_ip(result: dict) -> str | None:
    runtime_probe = result.get("observed_exit_ip") or result.get("probe_exit_ip")
    return str(runtime_probe).strip() if runtime_probe else None


def _extract_buttons(target: dict) -> list[dict]:
    raw_buttons = target.get("buttons") or []
    if not isinstance(raw_buttons, list):
        return []
    return [_normalize_button(raw, index) for index, raw in enumerate(raw_buttons) if isinstance(raw, dict)]


def _normalize_button(raw: dict, index: int) -> dict:
    text = str(raw.get("text") or "").strip()
    url = str(raw.get("url") or "").strip()
    effect = str(raw.get("effect") or raw.get("button_effect") or "").strip() or _infer_button_effect(text, url)
    return {
        "row": int(raw.get("row") or 0),
        "col": int(raw.get("col") or index),
        "text": text,
        "url": url,
        "effect": effect,
        "position": int(raw.get("position") or index + 1),
    }


def _infer_button_effect(text: str, url: str) -> str:
    text_lower = text.lower()
    if any(marker in text_lower for marker in ("下一页", "上一页", "next", "prev", "page", "页")):
        return "navigate_only"
    if not url:
        return "unknown"
    from urllib.parse import urlparse

    host = (urlparse(url).netloc or "").lower()
    if host in {"t.me", "telegram.me", "www.t.me", "www.telegram.me"}:
        return "join_candidate"
    return "external"


def _button_hash(button: dict) -> str:
    raw = f"{button.get('text', '')}:{button.get('url', '')}:{button.get('position', 0)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _random_dwell(payload: SearchRankDeboostPayload) -> int:
    dwell_min = int(payload.dwell_seconds_min or DEFAULT_DWELL_SECONDS_MIN)
    dwell_max = int(payload.dwell_seconds_max or DEFAULT_DWELL_SECONDS_MAX)
    if dwell_max <= dwell_min:
        return dwell_min
    return int(random.randint(dwell_min, dwell_max))


def _write_stat(
    session: Session,
    *,
    action: Action,
    payload: SearchRankDeboostPayload,
    target: dict,
    button: dict | None,
    dwell_seconds: int,
    join_button_detected: bool,
    joined: bool,
    skip_reason: str,
    hour_bucket: datetime,
    join_button_violation: bool = False,
) -> None:
    runtime = payload.runtime_environment or {}
    session.add(SearchRankDeboostActionStat(
        id=str(uuid4()),
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(action.account_id or 0),
        account_pool_id=int(payload.account_pool_id),
        proxy_airport_node_id=int(payload.proxy_airport_node_id) or None,
        observed_exit_ip=str(runtime.get("observed_exit_ip") or ""),
        bot_username=payload.bot_username,
        keyword_hash=payload.keyword_hash,
        competitor_group_username=str(target.get("username") or ""),
        competitor_group_peer_id=str(target.get("peer_id") or ""),
        competitor_group_title=str(target.get("title") or ""),
        competitor_position=int(target.get("position") or 0),
        button_hash=_button_hash(button) if button else "",
        button_effect=str(button.get("effect") or "") if button else "",
        join_button_detected=join_button_detected,
        joined=joined,
        dwell_seconds=dwell_seconds,
        hour_bucket=hour_bucket,
        captured_at=_now(),
        skip_reason=skip_reason,
        join_button_violation=join_button_violation,
    ))
    session.flush()


def _record_proxy_egress_alert(
    session: Session,
    *,
    action: Action,
    account: TgAccount,
    binding_id: int,
    probe_exit_ip: str | None,
) -> None:
    binding = session.get(AccountGroupProxyBinding, int(binding_id))
    binding_active = binding is not None and binding.status == "active"
    observed_exit_ip = (binding.observed_exit_ip or "") if binding else ""
    record_group_proxy_egress_failure_alert(
        session,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(account.id),
        binding_id=int(binding_id),
        binding_active=binding_active,
        observed_exit_ip=observed_exit_ip,
        probe_exit_ip=(probe_exit_ip or "").strip(),
    )


def _skip_result(action: Action, code: str, detail: str) -> dict:
    return {
        "success": False,
        "error_code": code,
        "error_message": detail,
        "skip_reason": code,
        "validation_stage": "search_rank_deboost",
    }


__all__ = ["execute_search_rank_deboost"]
