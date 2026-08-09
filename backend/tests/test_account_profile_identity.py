from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import TelegramDeveloperApp, Tenant, TgAccount, TgAccountProfileNameClaim
from app.schemas import TgAccountCreate, TgAccountProfileUpdate
from app.security import encrypt_secret
from app.services import accounts as accounts_service
from app.services.account_profile_identity import (
    DisplayNameConflict,
    NameClaimRequest,
    claim_profile_names,
    duplicate_name_groups,
    generate_unique_display_names,
    normalize_display_name,
    unavailable_name_keys,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.commit()
    return session


def _account(account_id: int, name: str, *, synced: bool = True, avatar: bool = True, age_days: int = 0) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=name,
        phone_masked=f"138****{account_id:04d}",
        profile_sync_status="已同步" if synced else "未同步",
        avatar_object_key=f"avatars/1/{account_id}/current.jpg" if avatar else "",
        created_at=datetime(2026, 1, 10) + timedelta(days=age_days),
    )


def test_normalize_display_name_collapses_unicode_and_invisible_variants():
    assert normalize_display_name("  Ａ\u200blice　Test  ") == "alice test"


def test_claim_profile_names_rejects_cross_account_normalized_duplicate():
    with _session() as session:
        session.add_all([_account(1, "旧名一"), _account(2, "旧名二")])
        session.commit()
        claim_profile_names(session, [NameClaimRequest(1, 1, "海盐 日记", "manual", "tester")])
        session.commit()

        with pytest.raises(DisplayNameConflict, match="display_name_conflict"):
            claim_profile_names(session, [NameClaimRequest(1, 2, "海盐　日记", "manual", "tester")])

        assert session.scalar(select(TgAccountProfileNameClaim.account_id)) == 1


def test_unavailable_names_include_current_accounts_and_historical_claims():
    with _session() as session:
        session.add_all([_account(1, "当前名字"), _account(2, "另一个名字")])
        session.commit()
        claim_profile_names(session, [NameClaimRequest(1, 1, "历史名字", "manual", "tester")])
        session.commit()

        keys = unavailable_name_keys(session, 1)

        assert normalize_display_name("当前名字") in keys
        assert normalize_display_name("历史名字") in keys


def test_local_generation_is_unique_across_existing_names_and_reproducible():
    blocked = {normalize_display_name("薄荷日记"), normalize_display_name("海盐日记")}

    first = generate_unique_display_names(500, blocked, "stable-seed")
    second = generate_unique_display_names(500, blocked, "stable-seed")

    assert first == second
    assert len({normalize_display_name(name) for name in first}) == 500
    assert not blocked & {normalize_display_name(name) for name in first}
    assert len({len(name) for name in first}) >= 4


def test_local_generation_excludes_forbidden_words():
    names = generate_unique_display_names(
        100,
        set(),
        "forbidden-seed",
        forbidden_words={"海盐", "便利店", "慢半拍"},
    )

    assert all(forbidden not in name for name in names for forbidden in {"海盐", "便利店", "慢半拍"})


def test_duplicate_groups_select_only_non_keeper_accounts():
    accounts = [
        _account(1, "海盐日记", synced=False, avatar=False, age_days=0),
        _account(2, "海盐日记", synced=True, avatar=False, age_days=2),
        _account(3, "海盐日记", synced=True, avatar=True, age_days=3),
        _account(4, "唯一名字"),
    ]

    groups = duplicate_name_groups(accounts)

    assert len(groups) == 1
    assert groups[0].keeper_account_id == 3
    assert groups[0].target_account_ids == (2, 1)


def test_account_creation_rejects_a_name_claimed_by_another_account():
    with _session() as session:
        session.add(
            TelegramDeveloperApp(
                id=1,
                app_name="测试应用",
                api_id=12345,
                api_hash_ciphertext=encrypt_secret("hash"),
                health_status="健康",
            )
        )
        session.commit()
        first = accounts_service.create_account(
            session,
            TgAccountCreate(tenant_id=1, display_name="唯一昵称", phone_number="+8613800010001"),
            "tester",
        )

        with pytest.raises(DisplayNameConflict, match="display_name_conflict"):
            accounts_service.create_account(
                session,
                TgAccountCreate(tenant_id=1, display_name="唯一昵称", phone_number="+8613800010002"),
                "tester",
            )

        assert first.display_name == "唯一昵称"


def test_manual_profile_update_rejects_a_name_claimed_by_another_account():
    with _session() as session:
        session.add_all([_account(1, "旧名一"), _account(2, "旧名二")])
        session.commit()
        accounts_service.update_account_profile(
            session,
            1,
            TgAccountProfileUpdate(display_name="唯一昵称", tg_first_name="拆分名", tg_last_name="不再保留"),
            "tester",
        )
        first = session.get(TgAccount, 1)
        assert first is not None
        assert first.tg_first_name == "唯一昵称"
        assert first.tg_last_name == ""

        with pytest.raises(DisplayNameConflict, match="display_name_conflict"):
            accounts_service.update_account_profile(
                session,
                2,
                TgAccountProfileUpdate(display_name="唯一昵称", tg_first_name="唯一昵称"),
                "tester",
            )
