from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import GroupSnapshot
from app.models import OperationTarget, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services import operation_target_peer_merge, operations


pytestmark = pytest.mark.no_postgres


def test_canonicalization_fetches_telegram_snapshot_before_serializable_transaction(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    calls: list[str] = []
    begin_transaction = operations.begin_peer_merge_transaction

    def record_transaction_start(session: Session) -> None:
        calls.append("serializable_transaction")
        begin_transaction(session)

    class FakeGateway:
        def resolve_group_by_public_username(self, *_args, **_kwargs):
            calls.append("telegram_snapshot")
            assert calls == ["telegram_snapshot"]
            return GroupSnapshot(
                tg_peer_id="-1003573333444",
                title="郑州楼凤",
                group_type="supergroup",
                member_count=1200,
                permission_label="可发言",
                can_send=True,
                username="zhengzhou167",
            )

    monkeypatch.setattr(operations, "begin_peer_merge_transaction", record_transaction_start)
    monkeypatch.setattr(operations, "gateway", FakeGateway())
    monkeypatch.setattr(operations, "credentials_for_account", lambda *_args, **_kwargs: None)

    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            OperationTarget(id=2785, tenant_id=1, target_type="group", tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤"),
            TgGroup(id=2806, tenant_id=1, tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤", can_send=True),
            TgAccount(id=11, tenant_id=1, display_name="观察账号", phone_masked="11", status="在线", session_ciphertext="session"),
            TgGroupAccount(tenant_id=1, group_id=2806, account_id=11, can_send=True),
        ])
        session.commit()

        result = operations.canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

    assert result["stable_peer_id"] == "-1003573333444"
    assert calls == ["telegram_snapshot", "serializable_transaction"]


def test_canonicalization_resolves_public_username_without_listing_all_dialogs(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    resolved_usernames: list[str] = []

    class FakeGateway:
        def resolve_group_by_public_username(self, _account_id, username, _session, _credentials):
            resolved_usernames.append(username)
            return GroupSnapshot("-1003573333444", "郑州楼凤", "supergroup", 1200, "可发言", True, username="zhengzhou167")

        def list_groups(self, *_args, **_kwargs):
            pytest.fail("canonicalization must not enumerate all Telegram dialogs")

    monkeypatch.setattr(operations, "gateway", FakeGateway())
    monkeypatch.setattr(operations, "credentials_for_account", lambda *_args, **_kwargs: None)

    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="默认运营空间"),
            OperationTarget(id=2785, tenant_id=1, target_type="group", tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤"),
            TgGroup(id=2806, tenant_id=1, tg_peer_id="https://t.me/zhengzhou167", title="郑州楼凤", can_send=True),
            TgAccount(id=11, tenant_id=1, display_name="观察账号", phone_masked="11", status="在线", session_ciphertext="session"),
            TgGroupAccount(tenant_id=1, group_id=2806, account_id=11, can_send=True),
        ])
        session.commit()

        result = operations.canonicalize_operation_target_peer(session, 1, 2785, 11, "tester")

    assert result["stable_peer_id"] == "-1003573333444"
    assert resolved_usernames == ["zhengzhou167"]


def test_runtime_config_reference_streams_action_payload_scan() -> None:
    payload_statements = []

    class RecordingSession:
        def execute(self, _statement):
            return ()

        def scalars(self, statement):
            payload_statements.append(statement)
            return iter([{"routing": {"target_group_ids": [2810]}}])

    referenced = operation_target_peer_merge._has_runtime_config_reference(RecordingSession(), 1, 2790, 2810)

    assert referenced is True
    assert payload_statements[0].get_execution_options()["yield_per"] == operation_target_peer_merge.ACTION_PAYLOAD_STREAM_BATCH_SIZE


def test_duplicate_group_link_lock_targets_only_group_accounts() -> None:
    statement = operation_target_peer_merge._duplicate_group_link_rows_statement(2810)

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE OF tg_group_accounts" in sql
