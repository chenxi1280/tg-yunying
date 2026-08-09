from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, AccountStatus, Tenant, TgAccount
from app.security import encrypt_session


pytestmark = pytest.mark.no_postgres


def _load_script():
    path = Path(__file__).resolve().parents[2] / ".github/scripts/account_profile_duplicate_reconcile.py"
    spec = spec_from_file_location("account_profile_duplicate_reconcile", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(AccountPool(id=1, tenant_id=1, name="普通账号池", pool_purpose="normal", is_default=True))
    session.commit()
    return session


def _account(account_id: int, name: str) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=1,
        account_identity="normal",
        display_name=name,
        phone_masked=f"138****{account_id:04d}",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext=encrypt_session(f"session-{account_id}"),
        profile_sync_status="已同步",
        avatar_object_key=f"avatars/1/{account_id}/current.jpg",
    )


def test_manifest_targets_only_duplicate_non_keepers_and_is_stable():
    script = _load_script()
    with _session() as session:
        session.add_all([_account(1, "海盐日记"), _account(2, "海盐日记"), _account(3, "唯一昵称")])
        session.commit()

        first = script.build_manifest(session, tenant_id=1, seed="fixed", deployed_sha="abc123")
        second = script.build_manifest(session, tenant_id=1, seed="fixed", deployed_sha="abc123")

    assert first == second
    assert first["duplicate_group_count"] == 1
    assert first["rename_target_count"] == 1
    assert first["keepers"] == [1]
    assert [target["account_id"] for target in first["targets"]] == [2]
    assert first["targets"][0]["new_display_name"] not in {"海盐日记", "唯一昵称"}
    assert script.manifest_sha256(first) == script.manifest_sha256(second)


def test_assert_unchanged_rejects_old_name_drift():
    script = _load_script()
    with _session() as session:
        session.add(_account(1, "已经变化"))
        session.commit()

        with pytest.raises(RuntimeError, match="target state drift"):
            script._assert_unchanged(
                session,
                1,
                [
                    {
                        "account_id": 1,
                        "old_display_name": "旧名字",
                        "old_profile_sync_status": "已同步",
                        "old_account_status": AccountStatus.ACTIVE.value,
                        "old_account_identity": "normal",
                    }
                ],
            )
