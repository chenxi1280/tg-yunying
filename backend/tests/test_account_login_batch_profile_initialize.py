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
from app.services.account_security import activate_account_security_batches


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
        phone_masked=f"138****{account_id:04d}",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext=encrypt_session("session"),
        profile_sync_status="已同步",
        avatar_object_key=f"avatars/1/{account_id}/current.png",
    )


def test_batch_payload_changes_only_name_profile_and_avatar():
    script = _load_script()
    script.APPROVAL_REF = "user-request-20260818"
    script.LOGIN_BATCH_IDS = (91,)
    script.EXPECTED_TARGET_COUNT = 1
    target = {
        "account_id": 1,
        "new_display_name": "橘猫收工",
        "old_tg_bio": "保留原简介",
        "avatar_source": "material:7",
    }

    payload = script._batch_payload([target], "a" * 64)

    assert payload.action_types == ["update_profile", "update_avatar"]
    assert payload.confirm_text == ""
    assert payload.profile_strategy.username_enabled is False
    assert payload.profile_strategy.overwrite_existing is True
    assert payload.preview_overrides[0].generated_display_name == "橘猫收工"
    assert payload.preview_overrides[0].generated_bio == "保留原简介"
    assert payload.preview_overrides[0].avatar_source == "material:7"


def test_existing_manifest_batch_requires_exact_name_and_avatar_source():
    script = _load_script()
    script.TENANT_ID = 1
    script.LOGIN_BATCH_IDS = (91,)
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
                remote_photo_id="991",
                perceptual_hash="0f0f0f0f0f0f0f0f",
            ),
        )
        monkeypatch.setattr(script, "_local_avatar_fingerprint", lambda _key: {
            "sha256": hashlib.sha256(b"local").hexdigest(),
            "perceptual_hash": "0f0f0f0f0f0f0f0f",
        })

        result = script._read_remote_result(session, item, account)

    assert result["status"] == "matched"
    assert result["remote_avatar_sha256"] == hashlib.sha256(b"remote").hexdigest()
    assert result["local_avatar_sha256"] == hashlib.sha256(b"local").hexdigest()
    assert result["avatar_perceptual_distance"] == 0


def test_remote_result_rejects_different_avatar_content(monkeypatch):
    script = _load_script()
    with _session() as session:
        account = _account()
        account.display_name = account.tg_first_name = "橘猫收工"
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
        monkeypatch.setattr(script.gateway, "pull_profile", lambda *_args: SimpleNamespace(first_name="橘猫收工", last_name=""))
        monkeypatch.setattr(script.gateway, "pull_profile_avatar_fingerprint", lambda *_args, **_kwargs: SimpleNamespace(
            sha256="a" * 64,
            size_bytes=100,
            remote_photo_id="992",
            perceptual_hash="ffffffffffffffff",
        ))
        monkeypatch.setattr(script, "_local_avatar_fingerprint", lambda _key: {
            "sha256": "b" * 64,
            "perceptual_hash": "0000000000000000",
        })

        result = script._read_remote_result(session, item, account)

    assert result["status"] == "mismatched"
    assert result["avatar_perceptual_distance"] == 64


def test_staged_batches_activate_together_and_claim_names():
    with _session() as session:
        session.add_all([_account(1), _account(2)])
        batches = [TgAccountSecurityBatch(tenant_id=1, status="ready") for _ in range(2)]
        session.add_all(batches)
        session.flush()
        session.add_all([
            TgAccountSecurityBatchItem(
                batch_id=batch.id,
                tenant_id=1,
                account_id=index,
                status="executable",
                generated_display_name=f"新名字{index}",
                trace_id=f"trace-{index}",
            )
            for index, batch in enumerate(batches, 1)
        ])
        session.commit()

        activate_account_security_batches(
            session,
            1,
            [int(batch.id) for batch in batches],
            actor="tester",
            confirm_text="确认",
        )

        assert {session.get(TgAccountSecurityBatch, batch.id).status for batch in batches} == {"running"}
        assert set(session.scalars(select(TgAccountSecurityBatchItem.status))) == {"pending"}
        assert set(session.scalars(select(TgAccountProfileNameClaim.name_key))) == {"新名字1", "新名字2"}


def test_cancelled_staged_batch_cannot_be_activated():
    with _session() as session:
        batch = TgAccountSecurityBatch(tenant_id=1, status="cancelled")
        session.add(batch)
        session.commit()

        with pytest.raises(ValueError, match="cannot be activated"):
            activate_account_security_batches(
                session,
                1,
                [int(batch.id)],
                actor="tester",
                confirm_text="确认",
            )


def test_apply_stages_every_chunk_before_activation(monkeypatch):
    script = _load_script()
    script.EXPECTED_SHA256 = "a" * 64
    script.EXPECTED_TARGET_COUNT = 2
    script.BATCH_SIZE = 1
    events: list[tuple[str, object]] = []

    class _Context:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(script, "SessionLocal", _Context)
    monkeypatch.setattr(script, "_existing_manifest_state", lambda *_args: ([], set()))
    monkeypatch.setattr(script, "_assert_no_conflicting_open_items", lambda *_args: None)
    monkeypatch.setattr(script, "_assert_targets_unchanged", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(script, "_ensure_neighbor_scope_audit", lambda *_args: events.append(("audit", None)))

    def create_batch(_session, _tenant_id, payload, _actor):
        events.append(("create", payload.confirm_text))
        return SimpleNamespace(id=len([event for event in events if event[0] == "create"]))

    def activate(_session, _tenant_id, batch_ids, **_kwargs):
        events.append(("activate", tuple(batch_ids)))

    monkeypatch.setattr(script, "create_account_security_batch", create_batch)
    monkeypatch.setattr(script, "activate_account_security_batches", activate)
    target = {
        "account_id": 1,
        "new_display_name": "名字",
        "old_tg_bio": "",
        "avatar_source": "material:7",
    }
    manifest = {"targets": [target, {**target, "account_id": 2}], "neighbor_scope": {}}

    result = script._apply(manifest, "a" * 64)

    assert result == [1, 2]
    assert events == [("create", ""), ("create", ""), ("audit", None), ("activate", (1, 2))]


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
