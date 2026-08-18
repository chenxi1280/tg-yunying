from __future__ import annotations

import hashlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountStatus,
    Tenant,
    TgAccount,
    TgAccountLoginBatchItem,
    TgAccountProfileNameClaim,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
)
from app.security import encrypt_session


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_script():
    path = PROJECT_ROOT / ".github/scripts/account_login_batch_profile_initialize.py"
    spec = spec_from_file_location("account_login_batch_profile_initialize", path)
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


def _account(account_id: int = 1) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=1,
        account_identity="normal",
        display_name="旧名字",
        tg_first_name="旧名字",
        phone_masked="138****0001",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext=encrypt_session("session"),
        profile_sync_status="已同步",
        avatar_object_key="avatars/1/1/current.png",
    )


def test_batch_payload_changes_only_name_profile_and_avatar():
    script = _load_script()
    script.APPROVAL_REF = "user-request-20260818"
    script.LOGIN_BATCH_ID = 91
    script.EXPECTED_TARGET_COUNT = 1
    target = {
        "account_id": 1,
        "new_display_name": "橘猫收工",
        "old_tg_bio": "保留原简介",
        "avatar_source": "material:7",
    }

    payload = script._batch_payload([target], "a" * 64)

    assert payload.action_types == ["update_profile", "update_avatar"]
    assert payload.profile_strategy.username_enabled is False
    assert payload.profile_strategy.overwrite_existing is True
    assert payload.preview_overrides[0].generated_display_name == "橘猫收工"
    assert payload.preview_overrides[0].generated_bio == "保留原简介"
    assert payload.preview_overrides[0].avatar_source == "material:7"


def test_existing_manifest_batch_requires_exact_name_and_avatar_source():
    script = _load_script()
    script.TENANT_ID = 1
    script.LOGIN_BATCH_ID = 91
    script.EXPECTED_TARGET_COUNT = 1
    script.EXPECTED_SHA256 = "a" * 64
    with _session() as session:
        batch = TgAccountSecurityBatch(
            tenant_id=1,
            reason=script._reason_prefix("a" * 64) + "approval",
        )
        session.add(batch)
        session.flush()
        session.add(TgAccountSecurityBatchItem(
            batch_id=batch.id,
            tenant_id=1,
            account_id=1,
            generated_display_name="橘猫收工",
            avatar_source="material:7",
        ))
        session.commit()

        batch_ids, account_ids = script._existing_manifest_state(session, [{
            "account_id": 1,
            "new_display_name": "橘猫收工",
            "avatar_source": "material:7",
        }])

    assert batch_ids == [batch.id]
    assert account_ids == {1}


def test_conflicting_open_profile_item_blocks_apply():
    script = _load_script()
    with _session() as session:
        batch = TgAccountSecurityBatch(tenant_id=1, reason="旧资料初始化")
        session.add(batch)
        session.flush()
        session.add(TgAccountSecurityBatchItem(
            batch_id=batch.id,
            tenant_id=1,
            account_id=1,
            status="pending",
        ))
        session.commit()

        with pytest.raises(RuntimeError, match="existing_profile_operation_conflict"):
            script._assert_no_conflicting_open_items(session, [{"account_id": 1}], set())


def test_remote_result_requires_name_claim_and_remote_avatar_fingerprint(monkeypatch):
    script = _load_script()
    with _session() as session:
        account = _account()
        account.display_name = "橘猫收工"
        account.tg_first_name = "橘猫收工"
        session.add(account)
        session.add(TgAccountProfileNameClaim(
            tenant_id=1,
            account_id=1,
            display_name="橘猫收工",
            name_key="橘猫收工",
            source="group_style_v2",
            created_by="tester",
        ))
        session.commit()
        item = TgAccountSecurityBatchItem(
            account_id=1,
            status="succeeded",
            profile_status="succeeded",
            avatar_status="succeeded",
            generated_display_name="橘猫收工",
        )
        monkeypatch.setattr(script, "credentials_for_account", lambda *_args: object())
        monkeypatch.setattr(
            script.gateway,
            "pull_profile",
            lambda *_args: SimpleNamespace(first_name="橘猫收工", last_name=""),
        )
        monkeypatch.setattr(
            script.gateway,
            "pull_profile_avatar_fingerprint",
            lambda *_args, **_kwargs: SimpleNamespace(
                sha256=hashlib.sha256(b"remote").hexdigest(),
                size_bytes=6,
            ),
        )
        monkeypatch.setattr(script, "_local_avatar_sha256", lambda _key: hashlib.sha256(b"local").hexdigest())

        result = script._read_remote_result(session, item, account)

    assert result["status"] == "matched"
    assert result["remote_avatar_sha256"] == hashlib.sha256(b"remote").hexdigest()
    assert result["local_avatar_sha256"] == hashlib.sha256(b"local").hexdigest()


def test_target_guard_uses_login_item_snapshot():
    script = _load_script()
    with _session() as session:
        account = _account()
        session.add(account)
        item = TgAccountLoginBatchItem(
            id=41,
            batch_id=91,
            tenant_id=1,
            line_no=1,
            phone_masked="138****0001",
            phone_fingerprint="phone-1",
            phone_fingerprint_version=1,
            phone_ciphertext="encrypted",
            code_source_host="code.example",
            code_source_uuid_fingerprint="uuid-1",
            code_source_uuid_hint="hint-1",
            route_hint="default",
            account_id=1,
            status="succeeded",
        )
        session.add(item)
        session.commit()
        target = {
            "account_id": 1,
            "login_item_id": 41,
            "login_item_state_version": item.state_version,
            "login_item_status": "succeeded",
            "old_display_name": "旧名字",
            "old_tg_first_name": "旧名字",
            "old_tg_last_name": "",
            "old_profile_sync_status": "已同步",
            "old_account_status": AccountStatus.ACTIVE.value,
            "old_account_identity": "normal",
            "old_pool_id": 1,
            "old_avatar_key_sha256": hashlib.sha256("avatars/1/1/current.png".encode()).hexdigest(),
        }

        script._assert_targets_unchanged(session, [target])
        session.execute(select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.id == 41)).scalar_one().state_version += 1
        with pytest.raises(RuntimeError, match="target state drift"):
            script._assert_targets_unchanged(session, [target])
