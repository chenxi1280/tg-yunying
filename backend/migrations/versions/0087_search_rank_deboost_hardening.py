"""harden search rank deboost persistence and account usage

Revision ID: 0087_rank_deboost_hardening
Revises: 0086_tenant_fixed_two_fa
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0087_rank_deboost_hardening"
down_revision = "0086_tenant_fixed_two_fa"
branch_labels = None
depends_on = None

VALID_ACCOUNT_USAGES = frozenset({"normal", "code_receiver", "rank_deboost"})
DEDICATED_ACCOUNT_USAGES = frozenset({"code_receiver", "rank_deboost"})
MISMATCH_USAGE = "account_purpose_mismatch"
REVALIDATION_ERROR = "migration_requires_gateway_revalidation"


def upgrade() -> None:
    _add_account_pool_columns()
    _add_group_binding_columns()
    _mark_bindings_needing_runtime_proxy()
    _create_group_binding_runtime_indexes()
    _create_click_reservations()
    _backfill_account_usage()
    _migrate_rank_task_scope()


def downgrade() -> None:
    _drop_table_if_exists("search_rank_deboost_click_reservations")
    _drop_index_if_exists("account_group_proxy_bindings", "uq_account_group_proxy_binding_active_node")
    _drop_column_if_exists("account_group_proxy_bindings", "last_probe_error")
    _drop_column_if_exists("account_group_proxy_bindings", "last_probe_at")
    _drop_foreign_key_column_if_exists("account_group_proxy_bindings", "runtime_proxy_id")
    _drop_column_if_exists("account_pools", "disable_reason")
    _drop_column_if_exists("account_pools", "disabled_by")
    _drop_column_if_exists("account_pools", "disabled_at")
    _drop_column_if_exists("account_pools", "is_enabled")


def _add_account_pool_columns() -> None:
    if not _has_table("account_pools"):
        return
    columns = [
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("disabled_at", sa.DateTime(), nullable=True),
        sa.Column("disabled_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("disable_reason", sa.String(length=255), nullable=False, server_default=""),
    ]
    for column in columns:
        _add_column_if_missing("account_pools", column)


def _add_group_binding_columns() -> None:
    if not _has_table("account_group_proxy_bindings"):
        return
    _add_foreign_key_column_if_missing(
        "account_group_proxy_bindings",
        sa.Column(
            "runtime_proxy_id",
            sa.Integer(),
            sa.ForeignKey("account_proxies.id", name="fk_account_group_binding_runtime_proxy"),
            nullable=True,
        ),
    )
    columns = [
        sa.Column("last_probe_at", sa.DateTime(), nullable=True),
        sa.Column("last_probe_error", sa.String(length=255), nullable=False, server_default=""),
    ]
    for column in columns:
        _add_column_if_missing("account_group_proxy_bindings", column)


def _create_group_binding_runtime_indexes() -> None:
    table_name = "account_group_proxy_bindings"
    if not _has_table(table_name):
        return
    columns = _column_names(table_name)
    if not {"tenant_id", "proxy_airport_node_id", "status", "unbound_at"} <= columns:
        return
    _create_partial_index_if_missing(
        "uq_account_group_proxy_binding_active_node",
        table_name,
        ["tenant_id", "proxy_airport_node_id", "status"],
        unique=True,
        where="status = 'active' AND unbound_at IS NULL",
    )


def _create_click_reservations() -> None:
    table_name = "search_rank_deboost_click_reservations"
    if _has_table(table_name):
        return
    op.create_table(
        table_name,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("account_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id"), nullable=False),
        sa.Column("keyword_hash", sa.String(length=64), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("hour_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reserved_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("consumed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="reserved"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("action_id", name="uq_rank_deboost_reservation_action"),
    )
    _create_reservation_indexes(table_name)


def _create_reservation_indexes(table_name: str) -> None:
    indexes = {
        "ix_rank_deboost_reservation_account_date_status": ["tenant_id", "account_id", "local_date", "status"],
        "ix_rank_deboost_reservation_account_keyword_date_status": [
            "tenant_id", "account_id", "keyword_hash", "local_date", "status",
        ],
        "ix_rank_deboost_reservation_pool_date_status": ["tenant_id", "account_pool_id", "local_date", "status"],
        "ix_rank_deboost_reservation_task_hour_status": ["task_id", "hour_bucket", "status"],
    }
    for name, columns in indexes.items():
        op.create_index(name, table_name, columns)


def _backfill_account_usage() -> None:
    if not _has_table("tg_accounts") or not _has_table("account_pools"):
        return
    bind = op.get_bind()
    metadata = sa.MetaData()
    accounts = sa.Table("tg_accounts", metadata, autoload_with=bind)
    pools = sa.Table("account_pools", metadata, autoload_with=bind)
    pool_rows = bind.execute(
        sa.select(pools.c.id, pools.c.tenant_id, pools.c.pool_purpose, pools.c.system_key)
    ).mappings()
    pool_by_id = {row["id"]: row for row in pool_rows}
    for account in bind.execute(sa.select(accounts.c.id, accounts.c.tenant_id, accounts.c.pool_id)).mappings():
        purpose = _backfilled_usage(account, pool_by_id.get(account["pool_id"]))
        bind.execute(accounts.update().where(accounts.c.id == account["id"]).values(account_identity=purpose))


def _backfilled_usage(account: sa.RowMapping, pool: sa.RowMapping | None) -> str:
    if pool is None or pool["tenant_id"] != account["tenant_id"]:
        return MISMATCH_USAGE
    purpose = str(pool["pool_purpose"] or "")
    system_key = str(pool["system_key"] or "")
    if purpose not in VALID_ACCOUNT_USAGES or not _pool_markers_consistent(purpose, system_key):
        return MISMATCH_USAGE
    return purpose


def _pool_markers_consistent(purpose: str, system_key: str) -> bool:
    if system_key in DEDICATED_ACCOUNT_USAGES:
        return system_key == purpose
    if purpose in DEDICATED_ACCOUNT_USAGES:
        return not system_key
    return True


def _migrate_rank_task_scope() -> None:
    if not _has_table("tasks"):
        return
    bind = op.get_bind()
    tasks = sa.Table("tasks", sa.MetaData(), autoload_with=bind)
    rows = bind.execute(sa.select(tasks).where(tasks.c.type == "search_rank_deboost")).mappings()
    for row in rows:
        values = _rank_task_migration_values(row)
        if values:
            bind.execute(tasks.update().where(tasks.c.id == row["id"]).values(**values))


def _rank_task_migration_values(row: sa.RowMapping) -> dict[str, object]:
    values: dict[str, object] = {}
    type_config = row["type_config"] if isinstance(row["type_config"], dict) else {}
    pool_id = type_config.get("account_pool_id")
    if pool_id:
        account_config = dict(row["account_config"] or {})
        account_config.update({"selection_mode": "group", "account_group_id": int(pool_id)})
        values["account_config"] = account_config
    if row["status"] == "running":
        values.update({"status": "paused", "last_error": REVALIDATION_ERROR})
    return values


def _mark_bindings_needing_runtime_proxy() -> None:
    table_name = "account_group_proxy_bindings"
    if not _has_table(table_name) or "runtime_proxy_id" not in _column_names(table_name):
        return
    bind = op.get_bind()
    bindings = sa.Table(table_name, sa.MetaData(), autoload_with=bind)
    bind.execute(
        bindings.update()
        .where(bindings.c.status == "active", bindings.c.runtime_proxy_id.is_(None))
        .values(status="needs_runtime_proxy")
    )


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _column_names(table_name):
        op.add_column(table_name, column)


def _add_foreign_key_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name in _column_names(table_name):
        return
    if op.get_bind().dialect.name != "sqlite":
        op.add_column(table_name, column)
        return
    with op.batch_alter_table(table_name, recreate="always") as batch_op:
        batch_op.add_column(column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and column_name in _column_names(table_name):
        op.drop_column(table_name, column_name)


def _drop_foreign_key_column_if_exists(table_name: str, column_name: str) -> None:
    if not _has_table(table_name) or column_name not in _column_names(table_name):
        return
    if op.get_bind().dialect.name != "sqlite":
        op.drop_column(table_name, column_name)
        return
    with op.batch_alter_table(table_name, recreate="always") as batch_op:
        batch_op.drop_column(column_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def _create_partial_index_if_missing(
    index_name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool,
    where: str,
) -> None:
    if index_name in _index_names(table_name):
        return
    op.create_index(
        index_name,
        table_name,
        columns,
        unique=unique,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if _has_table(table_name) and index_name in _index_names(table_name):
        op.drop_index(index_name, table_name=table_name)


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _has_table(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names()
