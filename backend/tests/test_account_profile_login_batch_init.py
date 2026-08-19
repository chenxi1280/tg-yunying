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
    AuditLog,
    AvatarMaterialSource,
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
    login_batch_neighbor_scope,
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


def _seed_login_batch(
    session: Session,
    count: int,
    batch_id: int = 91,
    *,
    start_id: int = 1,
    failed_count: int = 0,
    unresolved_count: int = 0,
    status: str = "completed",
    created_count: int | None = None,
) -> list[TgAccount]:
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
        for index in range(start_id, start_id + count)
    ]
    session.add_all(accounts)
    batch = _login_batch(
        batch_id,
        success_count=count,
        failed_count=failed_count,
        unresolved_count=unresolved_count,
        status=status,
    )
    session.add(batch)
    session.flush()
    success_items = [_login_item(batch_id, account.id, line_no) for line_no, account in enumerate(accounts, 1)]
    session.add_all(success_items)
    session.flush()
    audited_count = count if created_count is None else created_count
    session.add_all(_creation_audit(item) for item in success_items[:audited_count])
    session.add_all(
        _login_item(batch_id, None, count + offset, status="failed")
        for offset in range(1, failed_count + 1)
    )
    session.add_all(
        _login_item(batch_id, None, count + failed_count + offset, status="unresolved")
        for offset in range(1, unresolved_count + 1)
    )
    session.commit()
    return accounts


def _login_batch(
    batch_id: int,
    *,
    success_count: int,
    failed_count: int,
    unresolved_count: int,
    status: str,
) -> TgAccountLoginBatch:
    return TgAccountLoginBatch(
        id=batch_id,
        tenant_id=1,
        pool_id=1,
        created_by="tester",
        recipient_user_id=1,
        idempotency_key=f"batch-{batch_id}",
        request_fingerprint=f"fingerprint-{batch_id}",
        status=status,
        total_count=success_count + failed_count + unresolved_count,
        success_count=success_count,
        failed_count=failed_count,
        unresolved_count=unresolved_count,
        reason="测试批次",
        trace_id=f"trace-{batch_id}",
        finished_at=datetime(2026, 8, 18, 12, 0),
    )


def _login_item(
    batch_id: int,
    account_id: int | None,
    line_no: int,
    *,
    status: str = "succeeded",
) -> TgAccountLoginBatchItem:
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
        status=status,
        phase=status,
    )


def _creation_audit(item: TgAccountLoginBatchItem) -> AuditLog:
    return AuditLog(
        tenant_id=1,
        actor="tester",
        action="批量登录创建TG账号",
        target_type="tg_account",
        target_id=str(item.account_id),
        detail=f"batch_item_id={item.id}",
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
    session.add_all(_avatar_source(material_id) for material_id in range(1, 13))
    session.commit()


def _avatar_source(material_id: int) -> AvatarMaterialSource:
    return AvatarMaterialSource(
        tenant_id=1,
        material_id=material_id,
        source_page_id=f"avatar-{material_id}",
        source_page_url=f"https://commons.wikimedia.org/wiki/File:avatar-{material_id}",
        source_file_url=f"https://upload.wikimedia.org/avatar-{material_id}.png",
        license_code="CC0",
        license_url="https://creativecommons.org/publicdomain/zero/1.0/",
        attribution_text="CC0",
        content_sha256=f"{material_id:064x}",
        perceptual_hash=f"{material_id:016x}",
        contains_person=False,
        imported_by="tester",
    )


def _spec(count: int = 300, batch_ids: tuple[int, ...] = (91,)) -> LoginBatchInitializationSpec:
    return LoginBatchInitializationSpec(
        tenant_id=1,
        login_batch_ids=batch_ids,
        expected_target_count=count,
        style_group_ids=(11, 12),
        seed="profile-300-seed",
        deployed_sha="a" * 40,
        style_sample_cutoff_at=datetime.now().astimezone(),
    )


def test_manifest_freezes_exact_three_hundred_targets_without_raw_group_names():
    with _session() as session:
        accounts = _seed_login_batch(session, 200)
        accounts += _seed_login_batch(session, 100, batch_id=92, start_id=201)
        _seed_style_and_avatars(session, accounts[0].id)
        before_batches = session.scalar(select(func.count(TgAccountLoginBatch.id)))

        spec = _spec(batch_ids=(91, 92))
        first = build_login_batch_initialization_manifest(session, spec)
        second = build_login_batch_initialization_manifest(session, spec)
        after_batches = session.scalar(select(func.count(TgAccountLoginBatch.id)))

    encoded = json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert first == second
    assert len(first["targets"]) == 300
    assert first["login_batch_ids"] == [91, 92]
    assert first["style"]["sample_count"] == 100
    assert first["style"]["sample_cutoff_at"] == spec.style_sample_cutoff_at.isoformat()
    assert first["avatar_pool"]["unique_avatar_material_count"] == 12
    assert first["avatar_pool"]["max_material_assignment_count"] <= 30
    assert all(f"群友昵称{index}" not in encoded for index in range(120))
    assert manifest_sha256(first) == manifest_sha256(second)
    assert before_batches == after_batches == 2


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


def test_group_style_continues_sampling_after_duplicate_names():
    with _session() as session:
        accounts = _seed_login_batch(session, 1)
        session.add(TgGroup(id=11, tenant_id=1, tg_peer_id="peer-11", title="群11", listener_enabled=True))
        session.add_all([
            GroupContextMessage(
                tenant_id=1,
                group_id=11,
                listener_account_id=accounts[0].id,
                sender_peer_id=f"duplicate-sender-{index}",
                sender_name="重复昵称",
                content="测试消息",
                remote_message_id=f"duplicate-message-{index}",
            )
            for index in range(20)
        ])
        session.add_all([
            GroupContextMessage(
                tenant_id=1,
                group_id=11,
                listener_account_id=accounts[0].id,
                sender_peer_id=f"unique-sender-{index}",
                sender_name=f"唯一昵称{index}",
                content="测试消息",
                remote_message_id=f"unique-message-{index}",
            )
            for index in range(99)
        ])
        session.commit()

        evidence = build_group_style_evidence(session, 1, (11,))

    assert evidence.summary["sample_count"] == 100


def test_style_and_avatar_manifest_stays_stable_when_new_sources_arrive():
    with _session() as session:
        accounts = _seed_login_batch(session, 300)
        _seed_style_and_avatars(session, accounts[0].id)
        _seed_stable_avatar_prefix(session, accounts[0].id)
        session.commit()
        spec = _spec()
        first = build_login_batch_initialization_manifest(session, spec)
        _append_later_sources(session, accounts[0].id)
        session.commit()

        second = build_login_batch_initialization_manifest(session, spec)

    assert second == first


def _seed_stable_avatar_prefix(session: Session, listener_account_id: int) -> None:
    session.add_all([
        Material(
            id=material_id,
            tenant_id=1,
            title=f"稳定非真人头像 {material_id}",
            material_type="图片",
            content="",
            tags="avatar 头像",
            review_status="已审核",
            source_kind="upload",
            cache_ready_status="ready",
            tg_cache_account_id=listener_account_id,
            tg_cache_peer_id="cache-peer",
            tg_cache_message_id=f"stable-cache-{material_id}",
            mime_type="image/png",
        )
        for material_id in range(13, 313)
    ])
    session.add_all(_avatar_source(material_id) for material_id in range(13, 313))


def _append_later_sources(session: Session, listener_account_id: int) -> None:
    session.add_all([
        GroupContextMessage(
            tenant_id=1,
            group_id=11 + index % 2,
            listener_account_id=listener_account_id,
            sender_peer_id=f"later-sender-{index}",
            sender_name=f"后来昵称{index}",
            content="后续消息",
            remote_message_id=f"later-message-{index}",
        )
        for index in range(20)
    ])
    session.add(Material(
        id=999,
        tenant_id=1,
        title="后续非真人头像",
        material_type="图片",
        content="",
        tags="avatar 头像",
        review_status="已审核",
        source_kind="upload",
        cache_ready_status="ready",
        tg_cache_account_id=listener_account_id,
        tg_cache_peer_id="cache-peer",
        tg_cache_message_id="cache-later",
        mime_type="image/png",
    ))
    session.add(_avatar_source(999))


def test_discovers_unique_terminal_batch_set_with_three_hundred_success_accounts():
    with _session() as session:
        first = _seed_login_batch(session, 200, batch_id=91, failed_count=1)
        _seed_login_batch(session, 100, batch_id=92, start_id=201)
        duplicate_batch = _login_batch(
            93,
            success_count=4,
            failed_count=0,
            unresolved_count=1,
            status="completed_with_unresolved",
        )
        session.add(duplicate_batch)
        session.flush()
        duplicate_items = [
            _login_item(93, account.id, line_no)
            for line_no, account in enumerate(first[:4], 1)
        ]
        session.add_all(duplicate_items)
        session.add(_login_item(93, None, 5, status="unresolved"))
        session.commit()
        _seed_style_and_avatars(session, first[0].id)
        spec = LoginBatchInitializationSpec(
            tenant_id=1,
            login_batch_ids=(),
            expected_target_count=300,
            style_group_ids=(11, 12),
            seed="multi-batch-300",
            deployed_sha="b" * 40,
            style_sample_cutoff_at=datetime.now().astimezone(),
        )

        manifest = build_login_batch_initialization_manifest(session, spec)

    assert manifest["login_batch_ids"] == [93, 92, 91]
    assert len(manifest["targets"]) == 300
    assert {target["login_batch_id"] for target in manifest["targets"]} == {91, 92, 93}
    assert sum(batch_id == 93 for batch_id in (target["login_batch_id"] for target in manifest["targets"])) == 4
    assert max(target["account_id"] for target in manifest["targets"]) == 300


def test_explicit_mixed_scope_freezes_three_hundred_accounts():
    with _session() as session:
        first = _seed_login_batch(session, 191, batch_id=91)
        _seed_login_batch(session, 96, batch_id=92, start_id=192)
        _seed_login_batch(session, 15, batch_id=93, start_id=288, created_count=13)
        _seed_style_and_avatars(session, first[0].id)
        spec = LoginBatchInitializationSpec(
            tenant_id=1,
            login_batch_ids=(91, 92, 93),
            expected_target_count=300,
            style_group_ids=(11, 12),
            seed="mixed-scope-300",
            deployed_sha="c" * 40,
            created_only_batch_ids=(93,),
            style_sample_cutoff_at=datetime.now().astimezone(),
        )

        manifest = build_login_batch_initialization_manifest(session, spec)
        targets = load_login_batch_targets(session, spec)
        first_neighbor_scope = login_batch_neighbor_scope(session, targets)
        session.get(TgAccount, 301).display_name = "非目标账号人工变更"
        second_neighbor_scope = login_batch_neighbor_scope(session, targets)

    assert manifest["created_only_batch_ids"] == [93]
    assert len(manifest["targets"]) == 300
    assert sum(target["login_batch_id"] == 93 for target in manifest["targets"]) == 13
    assert max(target["account_id"] for target in manifest["targets"]) == 300
    assert manifest["neighbor_scope"]["account_count"] == 2
    assert first_neighbor_scope["state_sha256"] != second_neighbor_scope["state_sha256"]
