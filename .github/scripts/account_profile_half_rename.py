from __future__ import annotations

import json
import os
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, TgAccount, TgAccountSecurityBatch, TgAccountSecurityBatchItem
from app.schemas.account_security import (
    AccountSecurityBatchCreate,
    AccountSecurityProfileOverride,
    AvatarStrategy,
    ProfileGenerationStrategy,
)
from app.services.account_security import create_account_security_batch
from app.services.account_security.service import _execute_batch_item, _generate_profiles_from_local_pool


TENANT_ID = int(os.getenv("ACCOUNT_PROFILE_HALF_RENAME_TENANT_ID", "1"))
DRAIN_LIMIT = int(os.getenv("ACCOUNT_PROFILE_HALF_RENAME_DRAIN_LIMIT", "400"))
ACTOR = "github-actions-half-profile-rename"
REASON = "生产账号半数中文昵称重抽：去除雷同和中英混名"


def main() -> int:
    processed = 0
    with SessionLocal() as session:
        eligible_accounts = _eligible_accounts(session)
        target_success_count = _target_success_count(eligible_accounts)
        before_samples = _account_samples(eligible_accounts[:10])
    while processed < DRAIN_LIMIT:
        with SessionLocal() as session:
            summary = _operation_summary(session)
            if summary["success_count"] >= target_success_count:
                break
            batch_accounts = _next_batch_accounts(session, eligible_accounts, summary, target_success_count)
            if not batch_accounts:
                break
            batch_id = _create_batch(session, batch_accounts)
        processed += _drain_batch_items(batch_id, DRAIN_LIMIT - processed)
    with SessionLocal() as session:
        summary = _operation_summary(session)
        successful_accounts = _accounts_by_id(session, summary["success_account_ids"])
        payload = {
            "tenant_id": TENANT_ID,
            "eligible_account_count": len(eligible_accounts),
            "target_success_count": target_success_count,
            "successful_account_count": summary["success_count"],
            "attempted_account_count": summary["attempted_count"],
            "batch_ids": summary["batch_ids"],
            "processed_item_count": processed,
            "before_samples": before_samples,
            "after_success_samples": _account_samples(successful_accounts[:10]),
            "failed_items": _failed_item_samples(session),
        }
    print("ACCOUNT_PROFILE_HALF_RENAME=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    _assert_target_reached(payload)
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


def _target_success_count(accounts: list[TgAccount]) -> int:
    if not accounts:
        return 0
    return max(1, len(accounts) // 2)


def _operation_summary(session) -> dict[str, Any]:
    batch_ids = _operation_batch_ids(session)
    if not batch_ids:
        return {"batch_ids": [], "attempted_ids": set(), "success_account_ids": [], "success_count": 0, "attempted_count": 0}
    rows = list(
        session.execute(
            select(TgAccountSecurityBatchItem.account_id, TgAccountSecurityBatchItem.status)
            .where(TgAccountSecurityBatchItem.batch_id.in_(batch_ids))
            .order_by(TgAccountSecurityBatchItem.id.asc())
        )
    )
    attempted_ids = {int(account_id) for account_id, _status in rows}
    success_ids = [int(account_id) for account_id, status in rows if status == "succeeded"]
    return {
        "batch_ids": batch_ids,
        "attempted_ids": attempted_ids,
        "success_account_ids": success_ids,
        "success_count": len(success_ids),
        "attempted_count": len(attempted_ids),
    }


def _operation_batch_ids(session) -> list[int]:
    return list(
        session.scalars(
            select(TgAccountSecurityBatch.id)
            .where(
                TgAccountSecurityBatch.tenant_id == TENANT_ID,
                TgAccountSecurityBatch.created_by == ACTOR,
                TgAccountSecurityBatch.reason == REASON,
                TgAccountSecurityBatch.status != "cancelled",
            )
            .order_by(TgAccountSecurityBatch.id.asc())
        )
    )


def _next_batch_accounts(session, eligible_accounts: list[TgAccount], summary: dict[str, Any], target_success_count: int) -> list[TgAccount]:
    attempted_ids = set(summary["attempted_ids"])
    deficit = target_success_count - int(summary["success_count"])
    if deficit <= 0:
        return []
    candidate_ids = [account.id for account in eligible_accounts if account.id not in attempted_ids]
    return _accounts_by_id(session, candidate_ids[:deficit])


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
                TgAccountSecurityBatchItem.status == "pending",
            )
            .order_by(TgAccountSecurityBatchItem.id.asc())
        )


def _accounts_by_id(session, account_ids: list[int]) -> list[TgAccount]:
    if not account_ids:
        return []
    return list(session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)).order_by(TgAccount.id.asc())))


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


def _failed_item_samples(session) -> list[dict[str, Any]]:
    batch_ids = _operation_batch_ids(session)
    if not batch_ids:
        return []
    rows = list(
        session.scalars(
            select(TgAccountSecurityBatchItem)
            .where(
                TgAccountSecurityBatchItem.batch_id.in_(batch_ids),
                TgAccountSecurityBatchItem.status != "succeeded",
            )
            .order_by(TgAccountSecurityBatchItem.id.asc())
            .limit(20)
        )
    )
    return [
        {
            "account_id": item.account_id,
            "status": item.status,
            "profile_status": item.profile_status,
            "failure_type": item.failure_type,
            "failure_detail": item.failure_detail,
        }
        for item in rows
    ]


def _assert_target_reached(payload: dict[str, Any]) -> None:
    target = int(payload.get("target_success_count") or 0)
    success = int(payload.get("successful_account_count") or 0)
    if target == 0:
        raise RuntimeError("no eligible active accounts with sessions found")
    if success < target:
        raise RuntimeError("half account profile rename target was not reached")


if __name__ == "__main__":
    raise SystemExit(main())
