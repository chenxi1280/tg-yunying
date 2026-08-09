from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Tenant, TgAccount, TgAccountProfileNameClaim
from app.services.account_profile_identity import DisplayNameConflict, NameClaimRequest, claim_profile_names


def test_concurrent_name_claim_allows_only_one_account():
    with SessionLocal() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                TgAccount(id=1, tenant_id=1, display_name="旧名一", phone_masked="001"),
                TgAccount(id=2, tenant_id=1, display_name="旧名二", phone_masked="002"),
            ]
        )
        session.commit()

    barrier = Barrier(2)

    def claim(account_id: int) -> str:
        with SessionLocal() as session:
            barrier.wait()
            try:
                claim_profile_names(
                    session,
                    [NameClaimRequest(1, account_id, "并发唯一名", "test", f"worker-{account_id}")],
                )
                session.commit()
                return "success"
            except DisplayNameConflict:
                session.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(claim, [1, 2]))

    with SessionLocal() as session:
        claim_count = session.scalar(
            select(func.count(TgAccountProfileNameClaim.id)).where(
                TgAccountProfileNameClaim.tenant_id == 1,
                TgAccountProfileNameClaim.name_key == "并发唯一名",
            )
        )

    assert results == ["conflict", "success"]
    assert claim_count == 1
