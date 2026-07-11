from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, create_engine, func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountGroupProxyBinding, AccountPool
from app.schemas.accounts import AccountIdentityUpdate, AccountPoolCreate, AccountPoolOut, AccountPoolUpdate


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0087_search_rank_deboost_hardening.py"
RUNTIME_INDEX_MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0089_rank_deboost_runtime_index.py"


def _reservation_model():
    from app.models import SearchRankDeboostClickReservation

    return SearchRankDeboostClickReservation


def _migration_module():
    spec = importlib.util.spec_from_file_location("search_rank_deboost_hardening_0087", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_index_migration_module():
    spec = importlib.util.spec_from_file_location("rank_deboost_runtime_index_0089", RUNTIME_INDEX_MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("runtime index migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hardening_models_expose_pool_binding_and_reservation_fields() -> None:
    pool_columns = AccountPool.__table__.c
    binding_columns = AccountGroupProxyBinding.__table__.c
    reservation_columns = _reservation_model().__table__.c
    assert {"is_enabled", "disabled_at", "disabled_by", "disable_reason"} <= set(pool_columns.keys())
    assert {"runtime_proxy_id", "last_probe_at", "last_probe_error"} <= set(binding_columns.keys())
    runtime_proxy_fk = next(iter(binding_columns.runtime_proxy_id.foreign_keys))
    assert runtime_proxy_fk.target_fullname == "account_proxies.id"
    assert {
        "id", "tenant_id", "task_id", "action_id", "account_id", "account_pool_id",
        "keyword_hash", "local_date", "hour_bucket", "reserved_count", "consumed_count", "status", "expires_at",
    } == set(reservation_columns.keys())


def test_click_reservation_declares_global_action_constraint_and_quota_indexes() -> None:
    table = _reservation_model().__table__
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    indexes = {index.name: tuple(column.name for column in index.columns) for index in table.indexes}
    assert ("action_id",) in unique_columns
    assert indexes == {
        "ix_rank_deboost_reservation_account_date_status": ("tenant_id", "account_id", "local_date", "status"),
        "ix_rank_deboost_reservation_account_keyword_date_status": ("tenant_id", "account_id", "keyword_hash", "local_date", "status"),
        "ix_rank_deboost_reservation_pool_date_status": ("tenant_id", "account_pool_id", "local_date", "status"),
        "ix_rank_deboost_reservation_task_hour_status": ("task_id", "hour_bucket", "status"),
    }


def test_hardening_schema_contract_exposes_new_fields_without_manual_mismatch_assignment() -> None:
    from app.schemas.task_center import AccountGroupProxyBindingOut, SearchRankDeboostClickReservationOut

    assert {"is_enabled", "disabled_at", "disabled_by", "disable_reason"} <= set(AccountPoolOut.model_fields)
    assert {"is_enabled", "disable_reason"} <= set(AccountPoolUpdate.model_fields)
    assert {"pool_purpose", "system_key"}.isdisjoint(AccountPoolUpdate.model_fields)
    assert {"runtime_proxy_id", "last_probe_at", "last_probe_error"} <= set(AccountGroupProxyBindingOut.model_fields)
    assert {"action_id", "local_date", "hour_bucket", "reserved_count", "consumed_count", "status", "expires_at"} <= set(
        SearchRankDeboostClickReservationOut.model_fields
    )
    assert AccountIdentityUpdate(identity="normal").identity == "normal"
    assert AccountIdentityUpdate(identity="code_receiver").identity == "code_receiver"
    with pytest.raises(ValidationError):
        AccountIdentityUpdate(identity="rank_deboost")
    with pytest.raises(ValidationError):
        AccountIdentityUpdate(identity="account_purpose_mismatch")


def test_account_pool_schemas_reject_invalid_disable_lifecycle() -> None:
    with pytest.raises(ValidationError, match="disable_reason"):
        AccountPoolUpdate(disable_reason="x" * 256)
    with pytest.raises(ValidationError, match="default account pool must be enabled"):
        AccountPoolCreate(name="invalid", is_default=True, is_enabled=False)
    with pytest.raises(ValidationError, match="default account pool must be enabled"):
        AccountPoolUpdate(is_default=True, is_enabled=False)


def _legacy_metadata() -> MetaData:
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    Table("account_proxies", metadata, Column("id", Integer, primary_key=True))
    Table(
        "account_pools",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer),
        Column("pool_purpose", String(40)),
        Column("system_key", String(80)),
    )
    Table("tg_accounts", metadata, Column("id", Integer, primary_key=True), Column("tenant_id", Integer), Column("pool_id", Integer), Column("account_identity", String(40)))
    Table("tasks", metadata, Column("id", String(36), primary_key=True), Column("tenant_id", Integer), Column("type", String(30)), Column("status", String(20)), Column("last_error", String(255)), Column("account_config", JSON), Column("type_config", JSON))
    Table("actions", metadata, Column("id", String(36), primary_key=True))
    Table(
        "search_rank_deboost_action_stats",
        metadata,
        Column("id", String(36), primary_key=True),
        Column("tenant_id", Integer),
        Column("task_id", String(36)),
        Column("action_id", String(36)),
    )
    Table("proxy_airport_nodes", metadata, Column("id", Integer, primary_key=True))
    Table("account_group_proxy_bindings", metadata, Column("id", Integer, primary_key=True), Column("status", String(30)))
    return metadata


def _seed_legacy_rows(connection, metadata: MetaData) -> None:
    connection.execute(metadata.tables["tenants"].insert(), [{"id": 1}, {"id": 2}])
    connection.execute(metadata.tables["account_pools"].insert(), [
        {"id": 10, "tenant_id": 1, "pool_purpose": "normal", "system_key": ""},
        {"id": 11, "tenant_id": 1, "pool_purpose": "rank_deboost", "system_key": "rank_deboost"},
        {"id": 20, "tenant_id": 2, "pool_purpose": "code_receiver", "system_key": "code_receiver"},
        {"id": 21, "tenant_id": 1, "pool_purpose": "normal", "system_key": "rank_deboost"},
        {"id": 22, "tenant_id": 1, "pool_purpose": "rank_deboost", "system_key": "code_receiver"},
        {"id": 23, "tenant_id": 1, "pool_purpose": "code_receiver", "system_key": "legacy_conflict"},
    ])
    connection.execute(metadata.tables["tg_accounts"].insert(), [
        {"id": 1, "tenant_id": 1, "pool_id": 10, "account_identity": "rank_deboost"},
        {"id": 2, "tenant_id": 1, "pool_id": 11, "account_identity": "normal"},
        {"id": 3, "tenant_id": 1, "pool_id": 999, "account_identity": "normal"},
        {"id": 4, "tenant_id": 1, "pool_id": 20, "account_identity": "code_receiver"},
        {"id": 5, "tenant_id": 1, "pool_id": 21, "account_identity": "normal"},
        {"id": 6, "tenant_id": 1, "pool_id": 22, "account_identity": "rank_deboost"},
        {"id": 7, "tenant_id": 1, "pool_id": 23, "account_identity": "code_receiver"},
    ])
    connection.execute(metadata.tables["account_group_proxy_bindings"].insert(), [{"id": 1, "status": "active"}])
    connection.execute(metadata.tables["search_rank_deboost_action_stats"].insert(), [{
        "id": "historical-stat", "tenant_id": 1, "task_id": "rank-running", "action_id": "historical-action",
    }])
    connection.execute(metadata.tables["tasks"].insert(), [{
        "id": "rank-running", "tenant_id": 1, "type": "search_rank_deboost", "status": "running",
        "last_error": "", "account_config": {}, "type_config": {"account_pool_id": 11},
    }])


def test_migration_upgrades_sqlite_and_backfills_historical_state() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = _legacy_metadata()
    metadata.create_all(engine)
    migration = _migration_module()
    assert migration.down_revision == "0086_tenant_fixed_two_fa"
    with engine.begin() as connection:
        _seed_legacy_rows(connection, metadata)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        reflected = MetaData()
        reflected.reflect(connection)
        accounts = connection.execute(select(reflected.tables["tg_accounts"])).mappings().all()
        task = connection.execute(select(reflected.tables["tasks"])).mappings().one()
        binding = connection.execute(select(reflected.tables["account_group_proxy_bindings"])).mappings().one()
        reservation_count = connection.scalar(
            select(func.count()).select_from(reflected.tables["search_rank_deboost_click_reservations"])
        )
    assert {row["id"]: row["account_identity"] for row in accounts} == {
        1: "normal", 2: "rank_deboost", 3: "account_purpose_mismatch", 4: "account_purpose_mismatch",
        5: "account_purpose_mismatch", 6: "account_purpose_mismatch", 7: "account_purpose_mismatch",
    }
    assert task["status"] == "paused"
    assert task["last_error"] == "migration_requires_gateway_revalidation"
    assert task["account_config"] == {"selection_mode": "group", "account_group_id": 11}
    assert binding["status"] == "needs_runtime_proxy"
    assert reservation_count == 0
    assert inspect(engine).get_table_names().count("search_rank_deboost_click_reservations") == 1


def test_migration_downgrades_sqlite_runtime_proxy_foreign_key() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = _legacy_metadata()
    metadata.create_all(engine)
    migration = _migration_module()
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.downgrade()
    binding_columns = {item["name"] for item in inspect(engine).get_columns("account_group_proxy_bindings")}
    assert "runtime_proxy_id" not in binding_columns
    assert "search_rank_deboost_click_reservations" not in inspect(engine).get_table_names()


def test_migration_does_not_backfill_reservations_from_historical_stats() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = _legacy_metadata()
    metadata.create_all(engine)
    migration = _migration_module()
    with engine.begin() as connection:
        _seed_legacy_rows(connection, metadata)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        reservations = Table(
            "search_rank_deboost_click_reservations",
            MetaData(),
            autoload_with=connection,
        )
        reservation_count = connection.scalar(select(func.count()).select_from(reservations))
    assert reservation_count == 0


def test_migration_marks_duplicate_active_node_bindings_before_unique_index() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    Table("account_proxies", metadata, Column("id", Integer, primary_key=True))
    Table(
        "account_group_proxy_bindings",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("tenant_id", Integer),
        Column("account_pool_id", Integer),
        Column("proxy_airport_node_id", Integer),
        Column("status", String(30)),
        Column("unbound_at", String(30)),
    )
    migration = _runtime_index_migration_module()
    metadata.create_all(engine)
    with engine.begin() as connection:
        bindings = metadata.tables["account_group_proxy_bindings"]
        connection.execute(metadata.tables["tenants"].insert(), [{"id": 1}])
        connection.execute(
            bindings.insert(),
            [
                {"id": 1, "tenant_id": 1, "account_pool_id": 10, "proxy_airport_node_id": 20, "status": "active"},
                {"id": 2, "tenant_id": 1, "account_pool_id": 11, "proxy_airport_node_id": 20, "status": "active"},
            ],
        )
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        reflected = Table("account_group_proxy_bindings", MetaData(), autoload_with=connection)
        rows = connection.execute(select(reflected).order_by(reflected.c.id)).mappings().all()
        indexes = {item["name"] for item in inspect(connection).get_indexes("account_group_proxy_bindings")}

    assert [row["status"] for row in rows] == ["needs_runtime_proxy", "needs_runtime_proxy"]
    assert "uq_account_group_proxy_binding_active_node" in indexes


def test_reservation_defaults_are_persistable_on_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    model = _reservation_model()
    now = datetime.now()
    with Session(engine) as session:
        item = model(
            tenant_id=1, task_id="task", action_id="action", account_id=1, account_pool_id=2,
            keyword_hash="hash", local_date=date.today(), hour_bucket=now, expires_at=now + timedelta(minutes=10),
        )
        session.add(item)
        assert (item.reserved_count, item.consumed_count, item.status) == (None, None, None)
        session.flush()
        assert (item.reserved_count, item.consumed_count, item.status) == (1, 0, "reserved")


def test_duplicate_reservation_action_id_raises_integrity_error() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    model = _reservation_model()
    now = datetime.now()
    values = {
        "tenant_id": 1, "task_id": "task", "action_id": "duplicate-action", "account_id": 1,
        "account_pool_id": 2, "keyword_hash": "hash", "local_date": date.today(),
        "hour_bucket": now, "expires_at": now + timedelta(minutes=10),
    }
    with Session(engine) as session:
        session.add(model(**values))
        session.commit()
        session.add(model(**values))
        with pytest.raises(IntegrityError, match="UNIQUE constraint failed.*action_id"):
            session.flush()
