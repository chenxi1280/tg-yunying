from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest

from app.database import Base
from app.models import (
    AccountProxy,
    AuthorizationDrRuntimeContract,
    DeveloperAppSlotAssignment,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
)
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import (
    apply_online_abc_batch,
    preview_online_abc_batch,
    start_next_online_abc_item,
    sync_online_abc_batch,
)


pytestmark = pytest.mark.no_postgres
RELEASE_SHA = "a" * 40
ACCOUNT_IDS = list(range(101, 111))
MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/0162_authorization_online_abc_canary.py"


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed(db)
        yield db


def _seed(session: Session) -> None:
    session.add(Tenant(id=1, name="Online ABC"))
    session.add_all([
        TelegramDeveloperApp(id=1, app_name="A", api_id=1001, api_hash_ciphertext="a"),
        TelegramDeveloperApp(id=2, app_name="B", api_id=1002, api_hash_ciphertext="b"),
        TelegramDeveloperApp(id=3, app_name="C", api_id=1003, api_hash_ciphertext="c"),
    ])
    session.add_all([
        DeveloperAppSlotAssignment(
            slot_purpose="primary_sv", developer_app_id=1, assignment_version=1,
            credentials_version=1, assigned_by="admin",
        ),
        DeveloperAppSlotAssignment(
            slot_purpose="standby_1_sv", developer_app_id=2, assignment_version=1,
            credentials_version=1, assigned_by="admin",
        ),
        DeveloperAppSlotAssignment(
            slot_purpose="standby_2_my", developer_app_id=3, assignment_version=1,
            credentials_version=1, assigned_by="admin",
        ),
    ])
    session.add(AuthorizationDrRuntimeContract(id=1, mode="off", claim_scope_operation_id=""))
    for index, account_id in enumerate(ACCOUNT_IDS, start=1):
        _seed_account(session, account_id, index)
    session.commit()


def _seed_account(session: Session, account_id: int, index: int) -> None:
    proxy_id = 100 + index
    session.add(AccountProxy(
        id=proxy_id, tenant_id=1, name=f"sv-{index}", host="127.0.0.1",
        port=1000 + index, status="healthy",
    ))
    account = TgAccount(
        id=account_id, tenant_id=1, display_name=f"account-{index}",
        phone_masked=str(account_id), status="在线", session_ciphertext=f"a-{account_id}",
        developer_app_id=2, proxy_id=proxy_id,
    )
    session.add(account)
    session.flush()
    primary = TgAccountAuthorization(
        id=1000 + account_id, tenant_id=1, account_id=account_id,
        role="primary", logical_slot="primary", provision_region_code="sv",
        developer_app_id=2, proxy_id=proxy_id, session_ciphertext=f"a-{account_id}",
        status="active", health_status="healthy", is_current=True,
        is_slot_current=True, protected_from_cleanup=True,
    )
    source_c = TgAccountAuthorization(
        id=2000 + account_id, tenant_id=1, account_id=account_id,
        role="standby_2", logical_slot="standby_2", provision_region_code="sv",
        developer_app_id=3, proxy_id=proxy_id, session_ciphertext=f"c-{account_id}",
        status="active", health_status="healthy", is_current=False,
        is_slot_current=True, protected_from_cleanup=True,
    )
    session.add_all([primary, source_c])
    session.flush()
    account.current_authorization_id = primary.id


def test_preview_requires_exactly_ten_unique_targets(session: Session) -> None:
    with pytest.raises(AuthorizationDrError, match="Exactly 10"):
        preview_online_abc_batch(
            session, 1, ACCOUNT_IDS[:-1],
            idempotency_key="online-abc-10", deployed_release_sha=RELEASE_SHA,
        )


def test_migration_accepts_metadata_precreated_tables() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    spec = importlib.util.spec_from_file_location("online_abc_0162", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration_load_failed")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()


def test_apply_freezes_manifest_and_preserves_three_way_conservation(session: Session) -> None:
    preview = _preview(session)
    result = _apply(session, preview["fingerprint"])

    assert result["target_count"] == 10
    assert result["account_outcome_counts"] == {"pending": 10}
    assert result["standby_1_outcome_counts"] == {"pending": 10}
    assert result["standby_2_outcome_counts"] == {"pending": 10}
    assert result["conservation"]["valid"] is True
    assert [item["account_id"] for item in result["items"]] == ACCOUNT_IDS


def test_start_is_idempotent_and_serial(session: Session) -> None:
    batch_id = _apply(session, _preview(session)["fingerprint"])["batch_id"]

    first = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    repeated = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")

    assert first == repeated
    assert first["ordinal"] == 1
    assert first["account_id"] == ACCOUNT_IDS[0]
    assert first["c_idempotency_key"] == first["b_idempotency_key"] + ":c"


def test_apply_rejects_second_open_batch(session: Session) -> None:
    _apply(session, _preview(session)["fingerprint"])
    second = preview_online_abc_batch(
        session, 1, ACCOUNT_IDS,
        idempotency_key="online-abc-10-second", deployed_release_sha=RELEASE_SHA,
    )

    with pytest.raises(AuthorizationDrError, match="already open"):
        apply_online_abc_batch(
            session, 1, ACCOUNT_IDS, idempotency_key="online-abc-10-second",
            deployed_release_sha=RELEASE_SHA, expected_fingerprint=second["fingerprint"],
            requested_by="requester", approved_by="approver", approval_ref="ABC-10",
        )


def test_sync_completes_one_item_only_after_a_b_c_and_e4(session: Session) -> None:
    batch_id = _apply(session, _preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    _qualify_primary(session, command["account_id"])
    _add_operations(session, command, status="succeeded")

    result = sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-10")
    next_item = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")

    assert result["account_outcome_counts"] == {"pending": 9, "succeeded": 1}
    assert result["standby_1_outcome_counts"] == {"pending": 9, "succeeded": 1}
    assert result["standby_2_outcome_counts"] == {"pending": 9, "succeeded": 1}
    assert result["conservation"]["valid"] is True
    assert next_item["ordinal"] == 2


def test_reconcile_unknown_stops_entire_batch(session: Session) -> None:
    batch_id = _apply(session, _preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    _add_operation(session, command["account_id"], command["b_idempotency_key"], "reconcile_unknown")

    result = sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-10")

    assert result["status"] == "stopped"
    assert result["account_outcome_counts"] == {"pending": 9, "reconcile_unknown": 1}
    with pytest.raises(AuthorizationDrError, match="Batch is stopped"):
        start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")


def test_completed_primary_drift_stops_batch_before_next_account(session: Session) -> None:
    batch_id = _apply(session, _preview(session)["fingerprint"])["batch_id"]
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-10")
    _qualify_primary(session, command["account_id"])
    _add_operations(session, command, status="succeeded")
    sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-10")
    account = session.get(TgAccount, command["account_id"])
    account.status = "Session失效"
    session.commit()

    result = sync_online_abc_batch(session, batch_id, actor="approver", approval_ref="ABC-10")

    assert result["status"] == "stopped"
    assert result["account_outcome_counts"] == {"pending": 9, "primary_drift_after_success": 1}
    assert result["standby_1_outcome_counts"] == {"pending": 9, "succeeded": 1}
    assert result["standby_2_outcome_counts"] == {"pending": 9, "succeeded": 1}


def _preview(session: Session) -> dict:
    return preview_online_abc_batch(
        session, 1, ACCOUNT_IDS,
        idempotency_key="online-abc-10", deployed_release_sha=RELEASE_SHA,
    )


def _apply(session: Session, fingerprint: str) -> dict:
    return apply_online_abc_batch(
        session, 1, ACCOUNT_IDS, idempotency_key="online-abc-10",
        deployed_release_sha=RELEASE_SHA, expected_fingerprint=fingerprint,
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
    )


def _qualify_primary(session: Session, account_id: int) -> None:
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    account.authorization_fact_generation += 1
    primary.fact_version += 1
    primary.telegram_user_id_digest = "1" * 64
    primary.auth_key_fingerprint_digest = "2" * 64
    session.commit()


def _add_operations(session: Session, command: dict, *, status: str) -> None:
    account_id = command["account_id"]
    _add_operation(session, account_id, command["b_idempotency_key"], status)
    c_operation = _add_operation(
        session, account_id, f"migration-c-{account_id}", status,
        operation_type="migrate_standby_2",
    )
    migration_batch = TgAuthorizationDrBatch(
        tenant_id=1, idempotency_key=command["c_idempotency_key"],
        target_set_fingerprint="c" * 64, target_count=1, status=status,
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
    )
    session.add(migration_batch)
    session.flush()
    session.add(TgAuthorizationDrBatchItem(
        batch_id=migration_batch.id, tenant_id=1, account_id=account_id, ordinal=1,
        expected_source_authorization_id=2000 + account_id, expected_source_fact_version=1,
        expected_source_generation=1, target_generation=2, status=status, outcome=status,
        operation_id=c_operation.id,
    ))
    session.commit()
    _add_operation(session, account_id, command["e4_idempotency_key"], status)


def _add_operation(
    session: Session,
    account_id: int,
    key: str,
    status: str,
    *,
    operation_type: str = "",
):
    resolved_type = operation_type or (
        "abc_e4_primary_send" if key.endswith(":e4") else "provision_standby_1"
    )
    operation = TgAuthorizationDrOperation(
        tenant_id=1, account_id=account_id, operation_type=resolved_type,
        logical_slot="primary" if resolved_type.startswith("abc_e4") else "standby_2" if resolved_type.startswith("migrate") else "standby_1",
        source_generation=1, target_generation=1, developer_app_id=1,
        developer_app_api_id_snapshot=1001, developer_app_credentials_version=1,
        assignment_version=1, egress_id="sv-proxy:1", egress_version=1,
        idempotency_key=key, request_fingerprint="f" * 64, status=status,
        remote_call_state="unknown" if status == "reconcile_unknown" else "succeeded",
        requested_by="requester", approved_by="approver", approval_ref="ABC-10",
    )
    session.add(operation)
    session.commit()
    return operation
