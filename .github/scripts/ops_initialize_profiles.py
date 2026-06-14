from __future__ import annotations

import json
import re
from collections import Counter

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, Tenant, TgAccount
from app.schemas.account_security import AccountSecurityRetryRequest
from app.services.account_security import account_security_batch_detail, drain_account_security_batches, retry_account_security_batch


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
TENANT_ID = 1
PROFILE_BATCH_ID = 18
AVATAR_BATCH_ID = 19
PROFILE_ITEM_ID = 1240
AVATAR_ITEM_ID = 1242
ACTOR = "codex-prod-profile-init-20260614"


def has_cjk(value: str | None) -> bool:
    return bool(CJK_RE.search(value or ""))


def needs_initialization(account: TgAccount) -> bool:
    return (
        not has_cjk(account.tg_first_name)
        or not has_cjk(account.display_name)
        or (account.profile_sync_status or "") != "已同步"
        or not (account.username or "").strip()
        or not (account.avatar_object_key or "").strip()
    )


def remaining_reasons(account: TgAccount) -> list[str]:
    reasons = []
    if not has_cjk(account.tg_first_name) or not has_cjk(account.display_name):
        reasons.append("name_not_chinese")
    if (account.profile_sync_status or "") != "已同步":
        reasons.append("profile_not_synced")
    if not (account.username or "").strip():
        reasons.append("missing_username")
    if not (account.avatar_object_key or "").strip():
        reasons.append("missing_avatar")
    if account.status != AccountStatus.ACTIVE.value or not account.session_ciphertext:
        reasons.append("offline_or_no_session")
    return reasons


def batch_row(session, batch_id: int) -> dict:
    detail = account_security_batch_detail(session, TENANT_ID, batch_id)
    item_statuses = Counter(item.status for item in detail.items)
    attention_items = [
        {
            "item_id": item.id,
            "account_id": item.account_id,
            "status": item.status,
            "profile_status": item.profile_status,
            "avatar_status": item.avatar_status,
            "failure_type": item.failure_type,
            "failure_detail": (item.failure_detail or "")[:220],
            "next_retry_at": item.next_retry_at.isoformat() if item.next_retry_at else None,
        }
        for item in detail.items
        if item.id in {PROFILE_ITEM_ID, AVATAR_ITEM_ID} or item.status in {"failed", "waiting", "running", "pending"}
    ]
    return {
        "batch_id": batch_id,
        "status": detail.status,
        "total": detail.total_count,
        "success": detail.success_count,
        "skipped": detail.skipped_count,
        "failed": detail.failed_count,
        "item_statuses": dict(item_statuses),
        "attention_items": attention_items[:80],
    }


def print_batches(session, label: str) -> None:
    print(label, json.dumps([batch_row(session, PROFILE_BATCH_ID), batch_row(session, AVATAR_BATCH_ID)], ensure_ascii=False), flush=True)


def print_remaining(session) -> int:
    tenants = list(session.scalars(select(Tenant).order_by(Tenant.id.asc())))
    total_remaining = 0
    for tenant in tenants:
        accounts = list(
            session.scalars(
                select(TgAccount)
                .where(TgAccount.tenant_id == tenant.id, TgAccount.deleted_at.is_(None))
                .order_by(TgAccount.id.asc())
            )
        )
        remaining = [account for account in accounts if needs_initialization(account)]
        online_remaining = [account for account in remaining if account.status == AccountStatus.ACTIVE.value and account.session_ciphertext]
        total_remaining += len(remaining)
        reason_counts = Counter(reason for account in remaining for reason in remaining_reasons(account))
        online_reason_counts = Counter(reason for account in online_remaining for reason in remaining_reasons(account))
        status_counts = Counter(account.status for account in remaining)
        print(
            "REMAINING",
            json.dumps(
                {
                    "tenant_id": tenant.id,
                    "count": len(remaining),
                    "online_count": len(online_remaining),
                    "status_counts": dict(status_counts),
                    "reason_counts": dict(reason_counts),
                    "online_reason_counts": dict(online_reason_counts),
                    "account_ids": [account.id for account in remaining[:120]],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return total_remaining


def retry_single_item(batch_id: int, item_id: int) -> None:
    with SessionLocal() as session:
        retry_account_security_batch(
            session,
            TENANT_ID,
            batch_id,
            AccountSecurityRetryRequest(item_ids=[item_id]),
            actor=ACTOR,
        )
        print_batches(session, f"AFTER_RETRY_ITEM_{item_id}")
    processed = drain_account_security_batches(SessionLocal, limit=1)
    print("DRAIN_PROCESSED", json.dumps({"item_id": item_id, "processed": processed}, ensure_ascii=False), flush=True)
    with SessionLocal() as session:
        print_batches(session, f"AFTER_DRAIN_ITEM_{item_id}")


def main() -> None:
    with SessionLocal() as session:
        print_batches(session, "BEFORE_TARGETED_RETRY")
    retry_single_item(PROFILE_BATCH_ID, PROFILE_ITEM_ID)
    retry_single_item(AVATAR_BATCH_ID, AVATAR_ITEM_ID)
    with SessionLocal() as session:
        print_batches(session, "FINAL_BATCHES")
        remaining_total = print_remaining(session)
    print("FINAL_REMAINING_TOTAL", remaining_total, flush=True)


if __name__ == "__main__":
    main()
