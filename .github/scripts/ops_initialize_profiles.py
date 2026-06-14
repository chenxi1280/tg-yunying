from __future__ import annotations

import json
import re
from collections import Counter

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, Tenant, TgAccount
from app.services.account_security import account_security_batch_detail


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
TENANT_ID = 1
BATCH_IDS = [18, 19]


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


def batch_rows(session) -> list[dict]:
    rows = []
    for batch_id in BATCH_IDS:
        detail = account_security_batch_detail(session, TENANT_ID, batch_id)
        failure_types = Counter(item.failure_type for item in detail.items if item.failure_type)
        item_statuses = Counter(item.status for item in detail.items)
        attention_items = [
            {
                "item_id": item.id,
                "account_id": item.account_id,
                "status": item.status,
                "profile_status": item.profile_status,
                "username_status": item.username_status,
                "avatar_status": item.avatar_status,
                "failure_type": item.failure_type,
                "failure_detail": (item.failure_detail or "")[:260],
                "next_retry_at": item.next_retry_at.isoformat() if item.next_retry_at else None,
            }
            for item in detail.items
            if item.status in {"failed", "partial_success", "waiting", "running", "pending"}
        ]
        rows.append(
            {
                "batch_id": batch_id,
                "status": detail.status,
                "total": detail.total_count,
                "success": detail.success_count,
                "skipped": detail.skipped_count,
                "failed": detail.failed_count,
                "item_statuses": dict(item_statuses),
                "failure_types": dict(failure_types),
                "attention_items": attention_items[:80],
            }
        )
    return rows


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
                    "tenant_name": tenant.name,
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


def main() -> None:
    with SessionLocal() as session:
        print("QUERY_BATCHES", json.dumps(batch_rows(session), ensure_ascii=False), flush=True)
        remaining_total = print_remaining(session)
    print("FINAL_REMAINING_TOTAL", remaining_total, flush=True)


if __name__ == "__main__":
    main()
