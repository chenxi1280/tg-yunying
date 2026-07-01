from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, TgAccount, TgAccountSecurityBatchItem
from app.schemas.account_security import (
    AccountSecurityBatchCreate,
    AccountSecurityProfileOverride,
    AvatarStrategy,
    ProfileGenerationStrategy,
)
from app.services.account_security import account_security_batch_detail, create_account_security_batch
from app.services.account_security.service import _execute_batch_item, _generate_profiles_from_local_pool


TENANT_ID = int(os.getenv("ACCOUNT_PROFILE_HALF_RENAME_TENANT_ID", "1"))
DRAIN_LIMIT = int(os.getenv("ACCOUNT_PROFILE_HALF_RENAME_DRAIN_LIMIT", "400"))
ACTOR = "github-actions-half-profile-rename"
REASON = "生产账号半数中文昵称重抽：去除雷同和中英混名"


def main() -> int:
    with SessionLocal() as session:
        accounts = _eligible_accounts(session)
        selected = _selected_half(accounts)
        before = _account_samples(selected)
        batch_id = _create_batch(session, selected) if selected else 0
    processed = _drain_batch_items(batch_id, DRAIN_LIMIT) if batch_id else 0
    with SessionLocal() as session:
        batch = account_security_batch_detail(session, TENANT_ID, batch_id) if batch_id else None
        selected_after = _accounts_by_id(session, [account.id for account in selected])
        payload = {
            "tenant_id": TENANT_ID,
            "eligible_account_count": len(accounts),
            "selected_account_count": len(selected),
            "batch_id": batch_id,
            "processed_item_count": processed,
            "before_samples": before[:10],
            "after_samples": _account_samples(selected_after)[:10],
            "batch": _batch_summary(batch),
            "failed_items": _failed_item_samples(batch),
        }
    print("ACCOUNT_PROFILE_HALF_RENAME=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    _assert_batch_finished(payload)
    return 0


def _eligible_accounts(session) -> list[TgAccount]:
    return list(
        session.scalars(
            select(TgAccount)
            .where(
                TgAccount.tenant_id == TENANT_ID,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.session_ciphertext.is_not(None),
                TgAccount.session_ciphertext != "",
            )
            .order_by(TgAccount.id.asc())
        )
    )


def _selected_half(accounts: list[TgAccount]) -> list[TgAccount]:
    if not accounts:
        return []
    return accounts[: max(1, len(accounts) // 2)]


def _create_batch(session, accounts: list[TgAccount]) -> int:
    strategy = ProfileGenerationStrategy(
        generation_mode="local_random",
        language_style="中文",
        persona_style="自然用户",
        bio_enabled=False,
        username_enabled=False,
        overwrite_existing=True,
    )
    generated = _generate_profiles_from_local_pool(accounts, strategy)
    payload = AccountSecurityBatchCreate(
        account_ids=[account.id for account in accounts],
        action_types=["update_profile"],
        confirm_text="确认",
        reason=REASON,
        profile_strategy=strategy,
        avatar_strategy=AvatarStrategy(mode="none"),
        preview_overrides=[
            AccountSecurityProfileOverride(
                account_id=account.id,
                generated_display_name=str(item["display_name"]),
                generated_first_name=str(item["first_name"]),
                generated_last_name="",
                generated_bio=account.tg_bio or "",
            )
            for account, item in zip(accounts, generated, strict=True)
        ],
    )
    batch = create_account_security_batch(session, TENANT_ID, payload, ACTOR)
    return int(batch.id)


def _drain_batch_items(batch_id: int, limit: int) -> int:
    if limit <= 0:
        raise RuntimeError("ACCOUNT_PROFILE_HALF_RENAME_DRAIN_LIMIT must be positive")
    processed = 0
    while processed < limit:
        item_id = _next_pending_item_id(batch_id)
        if not item_id:
            return processed
        with SessionLocal() as session:
            _execute_batch_item(session, item_id)
        processed += 1
    return processed


def _next_pending_item_id(batch_id: int) -> int | None:
    with SessionLocal() as session:
        return session.scalar(
            select(TgAccountSecurityBatchItem.id)
            .where(
                TgAccountSecurityBatchItem.batch_id == batch_id,
                TgAccountSecurityBatchItem.status.in_(["pending", "waiting"]),
            )
            .order_by(TgAccountSecurityBatchItem.id.asc())
        )


def _accounts_by_id(session, account_ids: list[int]) -> list[TgAccount]:
    if not account_ids:
        return []
    rows = list(session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)).order_by(TgAccount.id.asc())))
    return rows


def _account_samples(accounts: list[TgAccount]) -> list[dict[str, Any]]:
    return [
        {
            "account_id": account.id,
            "display_name": account.display_name,
            "tg_first_name": account.tg_first_name,
            "tg_last_name": account.tg_last_name,
        }
        for account in accounts
    ]


def _batch_summary(batch) -> dict[str, Any]:
    if not batch:
        return {}
    return {
        "id": batch.id,
        "status": batch.status,
        "total_count": batch.total_count,
        "success_count": batch.success_count,
        "failed_count": batch.failed_count,
        "skipped_count": batch.skipped_count,
    }


def _failed_item_samples(batch) -> list[dict[str, Any]]:
    if not batch:
        return []
    return [
        {
            "account_id": item.account_id,
            "status": item.status,
            "profile_status": item.profile_status,
            "failure_type": item.failure_type,
            "failure_detail": item.failure_detail,
        }
        for item in batch.items
        if item.status != "succeeded"
    ][:20]


def _assert_batch_finished(payload: dict[str, Any]) -> None:
    batch = payload.get("batch") or {}
    selected = int(payload.get("selected_account_count") or 0)
    if selected == 0:
        raise RuntimeError("no eligible active accounts with sessions found")
    if int(batch.get("success_count") or 0) != selected:
        raise RuntimeError("half account profile rename did not fully succeed")


if __name__ == "__main__":
    raise SystemExit(main())
