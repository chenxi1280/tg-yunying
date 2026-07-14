from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import (
    AccountPool,
    AccountStatus,
    AiProvider,
    Task,
    Tenant,
    TenantAiSetting,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.security import encrypt_secret


def seed_group_accounts(
    session: Session,
    *,
    title: str,
    account_ids: Iterable[int],
    group_id: int = 7,
    topic_direction: str = "",
    normal_pool: bool = False,
    online_at: datetime | None = None,
) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    if normal_pool:
        session.add(AccountPool(id=1, tenant_id=1, name="普通账号组", pool_purpose="normal", is_default=True))
    session.add(TgGroup(
        id=group_id, tenant_id=1, tg_peer_id=f"-100{group_id}", title=title,
        auth_status="已授权运营", topic_direction=topic_direction,
    ))
    for account_id in account_ids:
        account = TgAccount(
            id=account_id, tenant_id=1, pool_id=1 if normal_pool else None,
            account_identity="normal",
            display_name=f"账号{account_id}", phone_masked=str(account_id),
            status=AccountStatus.ACTIVE.value, health_score=90,
            session_ciphertext=f"session-{account_id}",
        )
        session.add_all([account, TgGroupAccount(
            tenant_id=1, group_id=group_id, account_id=account_id, can_send=True,
        )])
        if online_at is not None:
            session.add(_online_state(account_id, online_at))


def add_ai_task(
    session: Session,
    *,
    task_id: str,
    name: str,
    account_ids: list[int],
    messages_per_round: int | None,
    type_overrides: dict | None = None,
    group_id: int = 7,
    selection_mode: str = "all",
) -> Task:
    type_config: dict = {
        "target_group_id": group_id,
        "messages_per_round_mode": "manual",
        "silent_mode_enabled": False,
    }
    if messages_per_round is not None:
        type_config["messages_per_round"] = messages_per_round
    type_config.update(type_overrides or {})
    account_config = {
        "selection_mode": selection_mode,
        "max_concurrent": len(account_ids) if selection_mode == "manual" else max(20, len(account_ids)),
        "cooldown_per_account_minutes": 0,
    }
    if selection_mode == "manual":
        account_config["account_ids"] = account_ids
    task = Task(
        id=task_id, tenant_id=1, name=name, type="group_ai_chat", status="running",
        account_config=account_config,
        pacing_config={
            "mode": "fixed", "interval_seconds_min": 0,
            "interval_seconds_max": 0, "jitter_percent": 0,
        },
        type_config=type_config,
    )
    session.add(task)
    return task


def add_ai_provider(
    session: Session,
    *,
    provider_id: int,
    provider_name: str,
    base_url: str,
    model_name: str,
    default: bool = False,
) -> None:
    session.add(AiProvider(
        id=provider_id, provider_name=provider_name,
        provider_type="openai_compatible", base_url=base_url,
        model_name=model_name, api_key_ciphertext=encrypt_secret(f"key-{provider_id}"),
        is_active=True, health_status="健康",
    ))
    if default:
        session.add(TenantAiSetting(
            tenant_id=1, default_provider_id=provider_id, ai_enabled=True,
            temperature=0.6, max_tokens=1024,
        ))


def _online_state(account_id: int, now: datetime) -> TgAccountOnlineState:
    return TgAccountOnlineState(
        tenant_id=1, account_id=account_id, desired_online=True,
        online_status="online", stale_after_at=now + timedelta(minutes=5),
    )
