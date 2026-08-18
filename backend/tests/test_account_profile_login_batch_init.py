from __future__ import annotations

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountPool,
    AccountStatus,
    GroupContextMessage,
    Material,
    Tenant,
    TgAccount,
    TgAccountLoginBatch,
    TgAccountLoginBatchItem,
    TgGroup,
)
from app.security import encrypt_session
from app.services.account_profile_login_batch_init import (
    LoginBatchInitializationSpec,
    build_group_style_evidence,
    build_login_batch_initialization_manifest,
    load_login_batch_targets,
    manifest_sha256,
    target_matches_manifest,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(AccountPool(id=1, tenant_id=1, name="普通账号池", pool_purpose="normal", is_default=True))
    session.commit()
    return session


def _seed_login_batch(session: Session, count: int, batch_id: int = 91) -> list[TgAccount]:
    accounts = [
        TgAccount(
            id=index,
            tenant_id=1,
            pool_id=1,
            account_identity="normal",
            display_name=f"待初始化账号-{index}",
            phone_masked=f"138****{index:04d}",
            status=AccountStatus.ACTIVE.value,
            session_ciphertext=encrypt_session(f"session-{index}"),
        )
        for index in range(1, count + 1)
    ]
    session.add_all(accounts)
    batch = TgAccountLoginBatch(
        id=batch_id,
        tenant_id=1,
        pool_id=1,
        created_by="tester",
        recipient_user_id=1,
        idempotency_key=f"batch-{batch_id}",
        request_fingerprint=f"fingerprint-{batch_id}",
        status="completed",
        total_count=count,
        success_count=count,
        reason="测试批次",
        trace_id=f"trace-{batch_id}",
        finished_at=datetime(2026, 8, 18, 12, 0),
    )
    session.add(batch)
    session.flush()
    session.add_all([_login_item(batch_id, account.id, line_no) for line_no, account in enumerate(accounts, 1)])
    session.commit()
    return accounts


def _login_item(batch_id: int, account_id: int, line_no: int) -> TgAccountLoginBatchItem:
    return TgAccountLoginBatchItem(
        batch_id=batch_id,
        tenant_id=1,
        line_no=line_no,
        phone_masked=f"138****{line_no:04d}",
        phone_fingerprint=f"phone-{batch_id}-{line_no}",
        phone_fingerprint_version=1,
        phone_ciphertext=f"encrypted-{line_no}",
        code_source_host="code.example",
        code_source_uuid_fingerprint=f"uuid-{batch_id}-{line_no}",
        code_source_uuid_hint=f"hint-{line_no}",
        route_hint="default",
        account_id=account_id,
        status="succeeded",
        phase="succeeded",
    )


def _seed_style_and_avatars(session: Session, listener_account_id: int) -> None:
    groups = [
        TgGroup(id=group_id, tenant_id=1, tg_peer_id=f"peer-{group_id}", title=f"群{group_id}", listener_enabled=True)
        for group_id in (11, 12)
    ]
    session.add_all(groups)
    session.add_all([
        GroupContextMessage(
            tenant_id=1,
            group_id=11 + index % 2,
            listener_account_id=listener_account_id,
            sender_peer_id=f"sender-{index}",
            sender_name=f"群友昵称{index}",
            content="测试消息",
            remote_message_id=f"message-{index}",
        )
        for index in range(120)
    ])
    session.add_all([
        Material(
            id=material_id,
            tenant_id=1,
            title=f"非真人头像 {material_id}",
            material_type="图片",
            content="",
            tags="avatar 头像",
            review_status="已审核",
            source_kind="upload",
            cache_ready_status="ready",
            tg_cache_account_id=listener_account_id,
            tg_cache_peer_id="cache-peer",
            tg_cache_message_id=f"cache-{material_id}",
            mime_type="image/png",
            usage_count=material_id % 3,
        )
        for material_id in range(1, 13)
    ])
    session.commit()


def _spec(count: int = 300, batch_id: int = 91) -> LoginBatchInitializationSpec:
    return LoginBatchInitializationSpec(
        tenant_id=1,
        login_batch_id=batch_id,
        expected_target_count=count,
        style_group_ids=(11, 12),
        seed="profile-300-seed",
        deployed_sha="a" * 40,
    )


def test_manifest_freezes_exact_three_hundred_targets_without_raw_group_names():
    with _session() as session:
        accounts = _seed_login_batch(session, 300)
        _seed_style_and_avatars(session, accounts[0].id)
        before_batches = session.scalar(select(func.count(TgAccountLoginBatch.id)))

        first = build_login_batch_initialization_manifest(session, _spec())
        second = build_login_batch_initialization_manifest(session, _spec())
        after_batches = session.scalar(select(func.count(TgAccountLoginBatch.id)))

    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert first == second
    assert len(first["targets"]) == 300
    assert first["style"]["sample_count"] == 120
    assert first["avatar_pool"]["unique_avatar_material_count"] == 12
    assert first["avatar_pool"]["max_material_assignment_count"] <= 30
    assert all(f"群友昵称{index}" not in encoded for index in range(120))
    assert manifest_sha256(first) == manifest_sha256(second)
    assert before_batches == after_batches == 1


def test_target_guard_rejects_login_item_or_account_drift():
    with _session() as session:
        accounts = _seed_login_batch(session, 1)
        targets = load_login_batch_targets(session, _spec(count=1))
        target = {
            "login_item_id": targets.items[0].id,
            "login_item_state_version": targets.items[0].state_version,
            "login_item_status": targets.items[0].status,
            "old_display_name": accounts[0].display_name,
            "old_tg_first_name": accounts[0].tg_first_name,
            "old_tg_last_name": accounts[0].tg_last_name,
            "old_profile_sync_status": accounts[0].profile_sync_status,
            "old_account_status": accounts[0].status,
            "old_account_identity": accounts[0].account_identity,
            "old_pool_id": accounts[0].pool_id,
            "old_avatar_key_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        }

        assert target_matches_manifest(accounts[0], targets.items[0], target)
        accounts[0].display_name = "人工改过的名字"
        assert not target_matches_manifest(accounts[0], targets.items[0], target)


def test_group_style_requires_one_hundred_anonymous_samples():
    with _session() as session:
        accounts = _seed_login_batch(session, 1)
        session.add(TgGroup(id=11, tenant_id=1, tg_peer_id="peer-11", title="群11", listener_enabled=True))
        session.add(
            GroupContextMessage(
                tenant_id=1,
                group_id=11,
                listener_account_id=accounts[0].id,
                sender_peer_id="sender-1",
                sender_name="只有一个样本",
                content="测试消息",
                remote_message_id="message-1",
            )
        )
        session.commit()

        with pytest.raises(RuntimeError, match="style_sample_insufficient"):
            build_group_style_evidence(session, 1, (11,))
