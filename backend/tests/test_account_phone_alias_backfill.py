from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AccountPool, AuditLog, Tenant, TgAccount, TgAccountPhoneFingerprintAlias
from app.security import encrypt_secret
from app.services._common import _now
from app.services.account_login import binding
from app.services.account_login.contracts import BatchLoginError
from app.services.account_login.preview import _missing_current_alias_count


pytestmark = pytest.mark.no_postgres


@pytest.fixture()
def session_factory(monkeypatch):
    settings = SimpleNamespace(account_batch_phone_fingerprint_versions="1")
    monkeypatch.setattr(binding, "get_settings", lambda: settings)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        session.add(Tenant(id=1, name="手机号别名回填测试租户"))
        session.add(AccountPool(id=10, tenant_id=1, name="目标分组", pool_purpose="normal"))
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
