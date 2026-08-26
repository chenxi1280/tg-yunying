from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

from app.database import Base
from app.models import TgAccount, TgAccountFullInitialization
from app.security import encrypt_session
from app.services._common import _now
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.parent import sync_parent_bindings
from tests.test_account_post_login_business_closure import _terminal_item
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres
MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations/versions/0168_account_post_login_full_init.py"
)


def test_0168_upgrade_is_idempotent_after_current_metadata_create_all(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_login_post_init_binding_owner")
        connection.exec_driver_sql("DROP INDEX ix_post_login_abc_request_status")
    spec = importlib.util.spec_from_file_location("migration_0168_review", MIGRATION_PATH)
    migration = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()
        assert "ix_login_post_init_binding_owner" in {
            index["name"]
            for index in inspect(connection).get_indexes("tg_account_login_post_init_bindings")
        }
        assert "ix_post_login_abc_request_status" in {
            index["name"]
            for index in inspect(connection).get_indexes("tg_post_login_abc_requests")
        }
    engine.dispose()


@pytest.mark.parametrize(
    ("status", "stage", "call_state", "failure_type"),
    [
        ("reconcile_unknown", "reconcile_unknown", "unknown", "two_fa_remote_unknown"),
        ("manual_required", "manual_required", "confirmed", "two_fa_email_confirmation_required"),
    ],
)
def test_expired_secret_is_kept_while_remote_two_fa_is_unresolved(
    session_factory,
    monkeypatch,
    status: str,
    stage: str,
    call_state: str,
    failure_type: str,
) -> None:
    from app.services.account_post_login_init import drain

    monkeypatch.setattr(
        drain,
        "get_settings",
        lambda: SimpleNamespace(
            account_post_login_init_mode="off",
            account_post_login_init_worker_concurrency=2,
        ),
    )
    with session_factory() as session:
        _, item = _new_login_item(session, f"protected-secret-{status}")
        owner = create_or_attach_full_initialization(
            session,
            item,
            actor="操作员",
            source_two_fa_kind="telegram_accepted",
            source_two_fa_password="source-password",
        )
        owner.status = status
        owner.stage = stage
        owner.two_fa_call_state = call_state
        owner.failure_type = failure_type
        owner.source_secret_expires_at = _now() - timedelta(seconds=1)
        session.commit()
        owner_id = owner.id

    assert drain.drain_account_post_login_initializations(session_factory, 1) == 0
    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, owner_id)
        assert owner.source_two_fa_password_ciphertext
        assert owner.source_secret_expires_at is not None


def test_reconcile_only_claims_explicit_reconcile_stage(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import drain

    calls: list[int] = []
    monkeypatch.setattr(
        drain,
        "get_settings",
        lambda: SimpleNamespace(
            account_post_login_init_mode="reconcile_only",
            account_post_login_init_worker_concurrency=2,
        ),
    )
    monkeypatch.setattr(
        drain,
        "execute_reconcile_stage",
        lambda _factory, claim: calls.append(claim.initialization_id),
    )
    with session_factory() as session:
        _, item = _new_login_item(session, "reconcile-only-explicit")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "pending"
        owner.stage = "reconcile"
        session.commit()
        owner_id = owner.id

    processed = drain.drain_account_post_login_initializations(
        session_factory,
        1,
        code_client=object(),
    )

    assert processed == 1
    assert calls == [owner_id]


@pytest.mark.parametrize("terminal_status", ["failed", "manual_required", "reconcile_unknown"])
def test_new_a_generation_does_not_reuse_terminal_debt(
    session_factory,
    terminal_status: str,
) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, f"old-debt-{terminal_status}")
        first = create_or_attach_full_initialization(session, first_item, actor="操作员")
        first.status = first.stage = terminal_status
        first.failure_type = f"old_{terminal_status}"
        account = session.get(TgAccount, 40)
        account.session_ciphertext = encrypt_session(f"new-session-{terminal_status}")
        _, second_item = _new_login_item(session, f"new-a-{terminal_status}")

        second = create_or_attach_full_initialization(session, second_item, actor="操作员")
        session.commit()

    assert second.id != first.id
    assert second.generation == first.generation + 1
    assert second.authorization_generation > first.authorization_generation
    assert first.status == terminal_status
    assert first.failure_type == f"old_{terminal_status}"


def test_parent_terminal_is_reprojected_when_manual_recovery_becomes_unknown(
    session_factory,
) -> None:
    with session_factory() as session:
        batch, item = _terminal_item(session, "manual-to-unknown")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.failure_type = "two_fa_current_password_unavailable"
        sync_parent_bindings(session, owner)
        owner.status = owner.stage = "reconcile_unknown"
        owner.failure_type = "two_fa_remote_unknown"
        owner.failure_detail = "remote result unknown"
        sync_parent_bindings(session, owner)
        session.commit()
        session.refresh(batch)

    assert item.status == "unresolved"
    assert item.failure_type == "two_fa_remote_unknown"
    assert batch.status == "completed_with_unresolved"
    assert batch.unresolved_count == 1
