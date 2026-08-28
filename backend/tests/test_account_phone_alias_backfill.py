from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AccountPool, AuditLog, TelegramDeveloperApp, Tenant, TgAccount, TgAccountPhoneFingerprintAlias
from app.schemas.accounts import TgAccountCreate
from app.security import encrypt_secret
from app.services._common import _now
from app.services import account_phone_aliases
from app.services.account_login import binding
from app.services.account_login.contracts import BatchLoginError
from app.services.account_login.preview import _missing_current_alias_count
from app.services.accounts import create_account, soft_delete_account


pytestmark = pytest.mark.no_postgres


@pytest.fixture()
def session_factory(monkeypatch):
    settings = SimpleNamespace(account_batch_phone_fingerprint_versions="1")
    monkeypatch.setattr(account_phone_aliases, "get_settings", lambda: settings)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        session.add(Tenant(id=1, name="手机号别名回填测试租户"))
        session.add(AccountPool(id=10, tenant_id=1, name="目标分组", pool_purpose="normal"))
        session.add(TelegramDeveloperApp(
            id=20,
            app_name="手机号别名测试应用",
            api_id=10020,
            api_hash_ciphertext=encrypt_secret("phone-alias-api-hash"),
            credentials_version=1,
        ))
        session.commit()
    yield factory
    engine.dispose()


def test_backfill_prefers_live_account_over_deleted_duplicate(session_factory) -> None:
    with session_factory() as session:
        deleted = TgAccount(
            tenant_id=1, pool_id=10, display_name="已删除历史账号", phone_masked="+120****0100",
            phone_ciphertext=encrypt_secret("+12025550100"), deleted_at=_now(),
        )
        live = TgAccount(
            tenant_id=1, pool_id=10, display_name="当前账号", phone_masked="+120****0101",
            phone_ciphertext=encrypt_secret("+12025550100"),
        )
        session.add_all([deleted, live])
        session.commit()
        preview = binding.backfill_phone_aliases(session, 1, apply=False)
        applied = binding.backfill_phone_aliases(
            session, 1, apply=True, actor="生产变更执行人", approval_ref="change-20260815",
        )
        aliases = list(session.scalars(select(TgAccountPhoneFingerprintAlias)))
        audit_log = session.scalar(select(AuditLog).where(AuditLog.action == "回填账号手机号别名"))
        missing_live_aliases = _missing_current_alias_count(session)

    assert preview == applied == {
        "scanned": 2,
        "created": 1,
        "conflicts": 0,
        "missing_phone": 0,
        "shadowed_deleted": 1,
    }
    assert len(aliases) == 1 and aliases[0].account_id == live.id
    assert audit_log is not None
    assert json.loads(audit_log.detail)["approval_ref"] == "change-20260815"
    assert missing_live_aliases == 0


def test_backfill_blocks_duplicate_live_accounts(session_factory) -> None:
    with session_factory() as session:
        session.add_all([
            TgAccount(
                tenant_id=1, pool_id=10, display_name="重复账号A", phone_masked="+120****0100",
                phone_ciphertext=encrypt_secret("+12025550100"),
            ),
            TgAccount(
                tenant_id=1, pool_id=10, display_name="重复账号B", phone_masked="+120****0101",
                phone_ciphertext=encrypt_secret("+12025550100"),
            ),
        ])
        session.commit()
        preview = binding.backfill_phone_aliases(session, 1, apply=False)
        with pytest.raises(BatchLoginError, match="手机号别名回填存在冲突"):
            binding.backfill_phone_aliases(
                session, 1, apply=True, actor="生产变更执行人", approval_ref="change-20260815",
            )
        aliases = list(session.scalars(select(TgAccountPhoneFingerprintAlias)))
        audit_log = session.scalar(select(AuditLog).where(AuditLog.action == "回填账号手机号别名"))

    assert preview["conflicts"] == 1
    assert aliases == []
    assert audit_log is None


def test_create_account_writes_phone_alias(session_factory) -> None:
    with session_factory() as session:
        account = create_account(
            session,
            TgAccountCreate(tenant_id=1, pool_id=10, display_name="新增账号", phone_number="+12025550123"),
            "tester",
        )
        aliases = list(session.scalars(select(TgAccountPhoneFingerprintAlias)))
        missing_live_aliases = _missing_current_alias_count(session)

    assert len(aliases) == 1
    assert aliases[0].account_id == account.id
    assert missing_live_aliases == 0


def test_batch_binding_allows_distinct_phones_with_same_mask(session_factory) -> None:
    first_item = SimpleNamespace(
        id=101,
        tenant_id=1,
        line_no=1,
        phone_masked="+191****3431",
        phone_ciphertext=encrypt_secret("+19112343431"),
        account_id=None,
    )
    second_item = SimpleNamespace(
        id=102,
        tenant_id=1,
        line_no=2,
        phone_masked="+191****3431",
        phone_ciphertext=encrypt_secret("+19199993431"),
        account_id=None,
    )
    with session_factory() as session:
        first = binding.bind_or_create_account(session, first_item, 10, "测试操作员")
        second = binding.bind_or_create_account(session, second_item, 10, "测试操作员")
        session.commit()
        aliases = list(session.scalars(select(TgAccountPhoneFingerprintAlias)))

    assert first.created is True and second.created is True
    assert first.account.id != second.account.id
    assert first.account.phone_masked == second.account.phone_masked
    assert len(aliases) == 2


def test_soft_delete_deactivates_phone_alias_for_reuse(session_factory) -> None:
    with session_factory() as session:
        first = create_account(
            session,
            TgAccountCreate(tenant_id=1, pool_id=10, display_name="待删除账号", phone_number="+12025550124"),
            "tester",
        )
        soft_delete_account(session, first.id, "tester", "测试复用手机号")
        second = create_account(
            session,
            TgAccountCreate(tenant_id=1, pool_id=10, display_name="重建账号", phone_number="+12025550124"),
            "tester",
        )
        alias = session.scalar(select(TgAccountPhoneFingerprintAlias))
        missing_live_aliases = _missing_current_alias_count(session)

    assert alias is not None
    assert alias.is_active is True
    assert alias.account_id == second.id
    assert missing_live_aliases == 0
