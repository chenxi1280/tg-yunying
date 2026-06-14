from __future__ import annotations

import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import TgAccount, TgAccountSecurityBatch, TgAccountSecurityBatchItem


ACCOUNT_ID = 334


def main() -> None:
    with SessionLocal() as session:
        account = session.get(TgAccount, ACCOUNT_ID)
        rows = []
        items = session.scalars(
            select(TgAccountSecurityBatchItem)
            .where(TgAccountSecurityBatchItem.account_id == ACCOUNT_ID)
            .order_by(TgAccountSecurityBatchItem.id.asc())
        )
        for item in items:
            batch = session.get(TgAccountSecurityBatch, item.batch_id)
            rows.append(
                {
                    "batch_id": item.batch_id,
                    "batch_status": batch.status if batch else "",
                    "item_id": item.id,
                    "item_status": item.status,
                    "profile_status": item.profile_status,
                    "username_status": item.username_status,
                    "avatar_status": item.avatar_status,
                    "failure_type": item.failure_type,
                    "failure_detail": (item.failure_detail or "")[:260],
                    "started_at": item.started_at.isoformat() if item.started_at else None,
                    "finished_at": item.finished_at.isoformat() if item.finished_at else None,
                    "next_retry_at": item.next_retry_at.isoformat() if item.next_retry_at else None,
                }
            )
        print(
            "ACCOUNT_334",
            json.dumps(
                {
                    "status": account.status if account else "",
                    "profile_sync_status": account.profile_sync_status if account else "",
                    "tg_first_name": account.tg_first_name if account else "",
                    "display_name": account.display_name if account else "",
                    "username": account.username if account else "",
                    "has_avatar": bool(account and account.avatar_object_key),
                    "items": rows,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
