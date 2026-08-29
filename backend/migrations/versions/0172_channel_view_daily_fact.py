"""Add daily view facts and a cross-Task daily identity owner.

Revision ID: 0172_channel_view_daily_fact
Revises: 0171_phone_mask_display_idx
"""

from alembic import op
import sqlalchemy as sa
from app.migration_sql_0172 import (
    ACTIVE_OWNER_BACKFILL_SQL as _ACTIVE_OWNER_BACKFILL_SQL,
    CONFIRMED_OWNER_BACKFILL_SQL as _CONFIRMED_OWNER_BACKFILL_SQL,
    VIEW_FACT_DATE_BACKFILL_SQL as _VIEW_FACT_DATE_BACKFILL_SQL,
)


revision = "0172_channel_view_daily_fact"
down_revision = "0171_phone_mask_display_idx"
branch_labels = None
depends_on = None

TABLE = "view_remote_facts"
OWNER_TABLE = "channel_view_daily_identity_owners"
FACT_ARCHIVE = "view_remote_fact_daily_rollback_archive"
OWNER_ARCHIVE = "channel_view_daily_owner_rollback_archive"
OLD_UQ = "uq_view_remote_fact_lifetime_source"
NEW_UQ = "uq_view_remote_fact_daily_source"
OWNER_UQ = "uq_channel_view_daily_identity"
FACT_OBLIGATION_FK = "fk_view_remote_fact_obligation_navigation"
INDEX_DATE = "ix_view_remote_facts_obligation_local_date"
INDEX_DATE_ACC = "ix_view_remote_facts_date_account"
EFFECT_KIND = "daily_view_operation"


def upgrade() -> None:
    _add_view_fact_columns()
    _replace_fact_obligation_navigation(nullable=True, ondelete="SET NULL")
    constraints = _constraint_names(TABLE)
    if OLD_UQ in constraints:
        op.drop_constraint(OLD_UQ, TABLE, type_="unique")
    if NEW_UQ not in constraints:
        op.create_unique_constraint(
            NEW_UQ,
            TABLE,
            ["target_peer_id", "channel_message_id", "account_id", "obligation_local_date"],
        )
    _add_view_fact_indexes()
    _restore_fact_archive()
    _create_owner_table()
    _restore_owner_archive()
    _backfill_daily_owners()


def downgrade() -> None:
    _assert_no_inflight_daily_owners()
    _assert_lifetime_fact_navigation_safe()
    _archive_daily_owners()
    _archive_daily_facts()
    _restore_lifetime_fact_contract()


def _add_view_fact_columns() -> None:
    columns = _column_names(TABLE)
    if "obligation_local_date" not in columns:
        op.add_column(TABLE, sa.Column("obligation_local_date", sa.Date(), nullable=True))
        op.execute(sa.text(_VIEW_FACT_DATE_BACKFILL_SQL))
        op.alter_column(TABLE, "obligation_local_date", nullable=False)
    if "remote_effect_kind" not in columns:
        op.add_column(
            TABLE,
            sa.Column(
                "remote_effect_kind",
                sa.String(length=40),
                nullable=False,
                server_default=EFFECT_KIND,
            ),
        )
    if "counter_increment_proven" not in columns:
        op.add_column(
            TABLE,
            sa.Column(
                "counter_increment_proven",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def _add_view_fact_indexes() -> None:
    indexes = _index_names(TABLE)
    if INDEX_DATE not in indexes:
        op.create_index(INDEX_DATE, TABLE, ["obligation_local_date"])
    if INDEX_DATE_ACC not in indexes:
        op.create_index(INDEX_DATE_ACC, TABLE, ["obligation_local_date", "account_id"])


def _create_owner_table() -> None:
    if OWNER_TABLE in _table_names():
        return
    op.create_table(
        OWNER_TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_peer_id", sa.String(length=120), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("obligation_local_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("logical_task_id", sa.String(length=36), nullable=False),
        sa.Column("obligation_id", sa.String(length=36), sa.ForeignKey("view_fulfillment_obligations.id", ondelete="SET NULL")),
        sa.Column("action_id", sa.String(length=36), sa.ForeignKey("actions.id", ondelete="SET NULL")),
        sa.Column("request_identity", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "target_peer_id",
            "channel_message_id",
            "account_id",
            "obligation_local_date",
            name=OWNER_UQ,
        ),
        sa.UniqueConstraint(
            "obligation_id",
            name="uq_channel_view_daily_identity_obligation",
        ),
        sa.UniqueConstraint(
            "action_id",
            name="uq_channel_view_daily_identity_action",
        ),
    )
    op.create_index(
        "ix_channel_view_daily_identity_state",
        OWNER_TABLE,
        ["obligation_local_date", "state"],
    )


def _backfill_daily_owners() -> None:
    op.execute(sa.text(_CONFIRMED_OWNER_BACKFILL_SQL))
    op.execute(sa.text(_ACTIVE_OWNER_BACKFILL_SQL))


def _assert_no_inflight_daily_owners() -> None:
    if OWNER_TABLE not in _table_names():
        return
    count = op.get_bind().execute(
        sa.text(
            f"SELECT COUNT(*) FROM {OWNER_TABLE} "
            "WHERE state IN ('pre_gateway','call_issued','unknown')"
        )
    ).scalar_one()
    if int(count):
        raise RuntimeError(f"channel_view_daily_owner_downgrade_inflight:{int(count)}")


def _assert_lifetime_fact_navigation_safe() -> None:
    count = _scalar_count(_FACT_MISSING_NAVIGATION_COUNT_SQL)
    if count:
        raise RuntimeError(f"channel_view_daily_fact_downgrade_navigation_missing:{count}")


def _archive_daily_owners() -> None:
    if OWNER_TABLE not in _table_names():
        return
    expected = _scalar_count(f"SELECT COUNT(*) FROM {OWNER_TABLE}")
    _create_owner_archive()
    op.execute(sa.text(_OWNER_ARCHIVE_INSERT_SQL))
    _assert_archive_count(
        "channel_view_daily_owner_archive_mismatch",
        expected,
        _scalar_count(_OWNER_ARCHIVE_READBACK_SQL),
    )
    op.drop_table(OWNER_TABLE)


def _archive_daily_facts() -> None:
    expected = _scalar_count(f"SELECT COUNT(*) FROM {TABLE}")
    _create_fact_archive()
    op.execute(sa.text(_FACT_ARCHIVE_INSERT_SQL))
    _assert_archive_count(
        "channel_view_daily_fact_archive_mismatch",
        expected,
        _scalar_count(_FACT_ARCHIVE_READBACK_SQL),
    )
    op.execute(sa.text(_FACT_DUPLICATE_DELETE_SQL))


def _restore_lifetime_fact_contract() -> None:
    indexes = _index_names(TABLE)
    if INDEX_DATE_ACC in indexes:
        op.drop_index(INDEX_DATE_ACC, table_name=TABLE)
    if INDEX_DATE in indexes:
        op.drop_index(INDEX_DATE, table_name=TABLE)
    constraints = _constraint_names(TABLE)
    if NEW_UQ in constraints:
        op.drop_constraint(NEW_UQ, TABLE, type_="unique")
    if OLD_UQ not in constraints:
        op.create_unique_constraint(
            OLD_UQ,
            TABLE,
            ["target_peer_id", "channel_message_id", "account_id"],
        )
    _replace_fact_obligation_navigation(nullable=False, ondelete="CASCADE")
    columns = _column_names(TABLE)
    for column in ("counter_increment_proven", "remote_effect_kind", "obligation_local_date"):
        if column in columns:
            op.drop_column(TABLE, column)


def _create_fact_archive() -> None:
    if FACT_ARCHIVE in _table_names():
        return
    op.create_table(
        FACT_ARCHIVE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("obligation_id", sa.String(length=36)),
        sa.Column("obligation_local_date", sa.Date(), nullable=False),
        sa.Column("target_peer_id", sa.String(length=120), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("remote_effect_kind", sa.String(length=40), nullable=False),
        sa.Column("counter_increment_proven", sa.Boolean(), nullable=False),
        sa.Column("remote_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def _create_owner_archive() -> None:
    if OWNER_ARCHIVE in _table_names():
        return
    op.create_table(
        OWNER_ARCHIVE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("target_peer_id", sa.String(length=120), nullable=False),
        sa.Column("channel_message_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("obligation_local_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("logical_task_id", sa.String(length=36), nullable=False),
        sa.Column("obligation_id", sa.String(length=36)),
        sa.Column("action_id", sa.String(length=36)),
        sa.Column("request_identity", sa.String(length=160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def _restore_fact_archive() -> None:
    if FACT_ARCHIVE not in _table_names():
        return
    expected = _scalar_count(f"SELECT COUNT(*) FROM {FACT_ARCHIVE}")
    op.execute(sa.text(_FACT_ARCHIVE_RESTORE_SQL))
    _assert_archive_count(
        "channel_view_daily_fact_restore_mismatch",
        expected,
        _scalar_count(_FACT_RESTORE_READBACK_SQL),
    )
    op.drop_table(FACT_ARCHIVE)


def _restore_owner_archive() -> None:
    if OWNER_ARCHIVE not in _table_names():
        return
    expected = _scalar_count(f"SELECT COUNT(*) FROM {OWNER_ARCHIVE}")
    op.execute(sa.text(_OWNER_ARCHIVE_RESTORE_SQL))
    _assert_archive_count(
        "channel_view_daily_owner_restore_mismatch",
        expected,
        _scalar_count(_OWNER_RESTORE_READBACK_SQL),
    )
    op.drop_table(OWNER_ARCHIVE)


def _constraint_names(table: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_unique_constraints(table)
    }


def _replace_fact_obligation_navigation(*, nullable: bool, ondelete: str) -> None:
    inspector = sa.inspect(op.get_bind())
    for item in inspector.get_foreign_keys(TABLE):
        if item.get("constrained_columns") == ["obligation_id"]:
            constraint_name = item.get("name")
            if not constraint_name:
                raise RuntimeError("channel_view_fact_obligation_fk_name_missing")
            op.drop_constraint(str(constraint_name), TABLE, type_="foreignkey")
    op.alter_column(
        TABLE,
        "obligation_id",
        existing_type=sa.String(length=36),
        nullable=nullable,
    )
    op.create_foreign_key(
        FACT_OBLIGATION_FK,
        TABLE,
        "view_fulfillment_obligations",
        ["obligation_id"],
        ["id"],
        ondelete=ondelete,
    )


def _index_names(table: str) -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _column_names(table: str) -> set[str]:
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(table)}


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _scalar_count(statement: str) -> int:
    return int(op.get_bind().execute(sa.text(statement)).scalar_one())


def _assert_archive_count(reason: str, expected: int, actual: int) -> None:
    if expected != actual:
        raise RuntimeError(f"{reason}:{expected}:{actual}")


_FACT_ARCHIVE_INSERT_SQL = """
INSERT INTO view_remote_fact_daily_rollback_archive (
    id, tenant_id, obligation_id, obligation_local_date, target_peer_id,
    channel_message_id, account_id, remote_effect_kind,
    counter_increment_proven, remote_confirmed_at, created_at
)
SELECT vrf.id, vrf.tenant_id, vrf.obligation_id, vrf.obligation_local_date,
       vrf.target_peer_id, vrf.channel_message_id, vrf.account_id,
       vrf.remote_effect_kind, vrf.counter_increment_proven,
       vrf.remote_confirmed_at, vrf.created_at
FROM view_remote_facts vrf
ON CONFLICT (id) DO NOTHING
"""

_FACT_ARCHIVE_READBACK_SQL = """
SELECT COUNT(*)
FROM view_remote_facts vrf
JOIN view_remote_fact_daily_rollback_archive archive ON archive.id = vrf.id
WHERE archive.tenant_id = vrf.tenant_id
  AND archive.obligation_id IS NOT DISTINCT FROM vrf.obligation_id
  AND archive.obligation_local_date = vrf.obligation_local_date
  AND archive.target_peer_id = vrf.target_peer_id
  AND archive.channel_message_id = vrf.channel_message_id
  AND archive.account_id = vrf.account_id
  AND archive.remote_effect_kind = vrf.remote_effect_kind
  AND archive.counter_increment_proven = vrf.counter_increment_proven
  AND archive.remote_confirmed_at = vrf.remote_confirmed_at
  AND archive.created_at = vrf.created_at
"""

_FACT_DUPLICATE_DELETE_SQL = """
WITH ranked AS (
    SELECT id, ROW_NUMBER() OVER (
        PARTITION BY target_peer_id, channel_message_id, account_id
        ORDER BY obligation_local_date, created_at, id
    ) AS row_rank
    FROM view_remote_facts
)
DELETE FROM view_remote_facts vrf
USING ranked
WHERE ranked.id = vrf.id AND ranked.row_rank > 1
"""

_FACT_ARCHIVE_RESTORE_SQL = """
INSERT INTO view_remote_facts (
    id, tenant_id, obligation_id, obligation_local_date, target_peer_id,
    channel_message_id, account_id, remote_effect_kind,
    counter_increment_proven, remote_confirmed_at, created_at
)
SELECT archive.id, archive.tenant_id, vfo.id, archive.obligation_local_date,
       archive.target_peer_id, archive.channel_message_id, archive.account_id,
       archive.remote_effect_kind, archive.counter_increment_proven,
       archive.remote_confirmed_at, archive.created_at
FROM view_remote_fact_daily_rollback_archive archive
LEFT JOIN view_fulfillment_obligations vfo ON vfo.id = archive.obligation_id
ON CONFLICT (id) DO UPDATE SET
    tenant_id = EXCLUDED.tenant_id,
    obligation_id = EXCLUDED.obligation_id,
    obligation_local_date = EXCLUDED.obligation_local_date,
    target_peer_id = EXCLUDED.target_peer_id,
    channel_message_id = EXCLUDED.channel_message_id,
    account_id = EXCLUDED.account_id,
    remote_effect_kind = EXCLUDED.remote_effect_kind,
    counter_increment_proven = EXCLUDED.counter_increment_proven,
    remote_confirmed_at = EXCLUDED.remote_confirmed_at,
    created_at = EXCLUDED.created_at
"""

_FACT_RESTORE_READBACK_SQL = """
SELECT COUNT(*)
FROM view_remote_fact_daily_rollback_archive archive
JOIN view_remote_facts vrf ON vrf.id = archive.id
WHERE archive.obligation_local_date = vrf.obligation_local_date
  AND archive.tenant_id = vrf.tenant_id
  AND vrf.obligation_id IS NOT DISTINCT FROM (
      SELECT vfo.id FROM view_fulfillment_obligations vfo
      WHERE vfo.id = archive.obligation_id
  )
  AND archive.target_peer_id = vrf.target_peer_id
  AND archive.channel_message_id = vrf.channel_message_id
  AND archive.account_id = vrf.account_id
  AND archive.remote_effect_kind = vrf.remote_effect_kind
  AND archive.counter_increment_proven = vrf.counter_increment_proven
  AND archive.remote_confirmed_at = vrf.remote_confirmed_at
  AND archive.created_at = vrf.created_at
"""

_OWNER_ARCHIVE_INSERT_SQL = """
INSERT INTO channel_view_daily_owner_rollback_archive (
    id, tenant_id, target_peer_id, channel_message_id, account_id,
    obligation_local_date, state, logical_task_id, obligation_id, action_id,
    request_identity, version, created_at, updated_at
)
SELECT id, tenant_id, target_peer_id, channel_message_id, account_id,
       obligation_local_date, state, logical_task_id, obligation_id, action_id,
       request_identity, version, created_at, updated_at
FROM channel_view_daily_identity_owners
ON CONFLICT (id) DO NOTHING
"""

_OWNER_ARCHIVE_READBACK_SQL = """
SELECT COUNT(*)
FROM channel_view_daily_identity_owners owner
JOIN channel_view_daily_owner_rollback_archive archive ON archive.id = owner.id
WHERE archive.target_peer_id = owner.target_peer_id
  AND archive.tenant_id = owner.tenant_id
  AND archive.channel_message_id = owner.channel_message_id
  AND archive.account_id = owner.account_id
  AND archive.obligation_local_date = owner.obligation_local_date
  AND archive.state = owner.state
  AND archive.logical_task_id = owner.logical_task_id
  AND archive.obligation_id IS NOT DISTINCT FROM owner.obligation_id
  AND archive.action_id IS NOT DISTINCT FROM owner.action_id
  AND archive.request_identity = owner.request_identity
  AND archive.version = owner.version
  AND archive.created_at = owner.created_at
  AND archive.updated_at = owner.updated_at
"""

_OWNER_ARCHIVE_RESTORE_SQL = """
INSERT INTO channel_view_daily_identity_owners (
    id, tenant_id, target_peer_id, channel_message_id, account_id,
    obligation_local_date, state, logical_task_id, obligation_id, action_id,
    request_identity, version, created_at, updated_at
)
SELECT archive.id, archive.tenant_id, archive.target_peer_id,
       archive.channel_message_id, archive.account_id,
       archive.obligation_local_date, archive.state, archive.logical_task_id,
       vfo.id, action.id, archive.request_identity, archive.version,
       archive.created_at, archive.updated_at
FROM channel_view_daily_owner_rollback_archive archive
LEFT JOIN view_fulfillment_obligations vfo ON vfo.id = archive.obligation_id
LEFT JOIN actions action ON action.id = archive.action_id
"""

_OWNER_RESTORE_READBACK_SQL = """
SELECT COUNT(*)
FROM channel_view_daily_owner_rollback_archive archive
JOIN channel_view_daily_identity_owners owner ON owner.id = archive.id
WHERE archive.target_peer_id = owner.target_peer_id
  AND archive.tenant_id = owner.tenant_id
  AND archive.channel_message_id = owner.channel_message_id
  AND archive.account_id = owner.account_id
  AND archive.obligation_local_date = owner.obligation_local_date
  AND archive.state = owner.state
  AND archive.logical_task_id = owner.logical_task_id
  AND owner.obligation_id IS NOT DISTINCT FROM (
      SELECT vfo.id FROM view_fulfillment_obligations vfo
      WHERE vfo.id = archive.obligation_id
  )
  AND owner.action_id IS NOT DISTINCT FROM (
      SELECT action.id FROM actions action WHERE action.id = archive.action_id
  )
  AND archive.request_identity = owner.request_identity
  AND archive.version = owner.version
  AND archive.created_at = owner.created_at
  AND archive.updated_at = owner.updated_at
"""

_FACT_MISSING_NAVIGATION_COUNT_SQL = """
SELECT COUNT(*)
FROM view_remote_facts vrf
LEFT JOIN view_fulfillment_obligations vfo ON vfo.id = vrf.obligation_id
WHERE vrf.obligation_id IS NULL OR vfo.id IS NULL
"""
