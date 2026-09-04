"""Add typed account fleet activity policies and ledgers.

Revision ID: 0211_account_fleet_activity
Revises: 0210_cross_adapter_journey
"""

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "0211_account_fleet_activity"
down_revision = "0210_cross_adapter_journey"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_policies()
    _create_ledgers()
    _create_projections()
    _backfill_pool_policies()
    _backfill_projection_states()


def _create_policies() -> None:
    table = "account_fleet_activity_policy_revisions"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("period_kind", sa.String(32), nullable=False, server_default="calendar_day"),
        sa.Column("rolling_window_days", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("required_activity_classes", sa.JSON(), nullable=False),
        sa.Column("class_targets", sa.JSON(), nullable=False),
        sa.Column("union_policy", sa.String(64), nullable=False),
        sa.Column("classification_policy_revision", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "account_pool_id", "revision", name="uq_fleet_activity_policy_revision"),
    )
    op.create_index(
        "uq_fleet_activity_policy_active", table,
        ["tenant_id", "account_pool_id"], unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def _create_ledgers() -> None:
    table = "account_fleet_activity_ledgers"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("policy_revision_id", sa.String(36), sa.ForeignKey("account_fleet_activity_policy_revisions.id"), nullable=False),
        sa.Column("period_kind", sa.String(32), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("activity_counts", sa.JSON(), nullable=False),
        sa.Column("latest_activity_at", sa.JSON(), nullable=False),
        sa.Column("qualified_activity_classes", sa.JSON(), nullable=False),
        sa.Column("required_status", sa.JSON(), nullable=False),
        sa.Column("fairness_debt", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "account_pool_id", "account_id", "period_start", "period_end", name="uq_fleet_activity_account_period"),
    )
    op.create_index(
        "ix_fleet_activity_pool_period", table,
        ["tenant_id", "account_pool_id", "period_start", "account_id"],
    )


def _create_projections() -> None:
    table = "account_fleet_activity_fact_projections"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ledger_id", sa.String(36), sa.ForeignKey("account_fleet_activity_ledgers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("policy_revision_id", sa.String(36), sa.ForeignKey("account_fleet_activity_policy_revisions.id"), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=False),
        sa.Column("action_id", sa.String(36), nullable=False),
        sa.Column("activity_class", sa.String(48), nullable=False),
        sa.Column("source_fact_kind", sa.String(48), nullable=False),
        sa.Column("source_fact_id", sa.String(80), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("account_pool_id", "account_id", "activity_class", "source_fact_kind", "source_fact_id", name="uq_fleet_activity_source_fact"),
    )
    op.create_index(
        "ix_fleet_activity_projection_timeline", table,
        ["tenant_id", "account_pool_id", "account_id", "observed_at"],
    )


def _backfill_pool_policies() -> None:
    bind = op.get_bind()
    pools = sa.table(
        "account_pools",
        sa.column("id", sa.Integer()),
        sa.column("tenant_id", sa.Integer()),
        sa.column("pool_purpose", sa.String()),
    )
    policies = sa.table(
        "account_fleet_activity_policy_revisions",
        sa.column("id", sa.String()), sa.column("tenant_id", sa.Integer()),
        sa.column("account_pool_id", sa.Integer()), sa.column("revision", sa.Integer()),
        sa.column("period_kind", sa.String()), sa.column("rolling_window_days", sa.Integer()),
        sa.column("required_activity_classes", sa.JSON()), sa.column("class_targets", sa.JSON()),
        sa.column("union_policy", sa.String()), sa.column("classification_policy_revision", sa.String()),
        sa.column("state", sa.String()), sa.column("effective_from", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(sa.select(pools.c.id, pools.c.tenant_id).where(
        pools.c.pool_purpose == "normal"
    )).all()
    now_value = datetime.now(timezone.utc)
    for pool_id, tenant_id in rows:
        bind.execute(policies.insert().values(**_policy_values(pool_id, tenant_id, now_value)))


def _policy_values(pool_id: int, tenant_id: int, now_value: datetime) -> dict:
    classes = ("passive_operation", "visible_reaction", "authored_content", "human_linked_interaction")
    targets = {
        "any_confirmed_business_operation": {"minimum_facts": 1, "window_days": 3},
        **{item: {"minimum_facts": 0, "window_days": 3} for item in classes},
    }
    return {
        "id": str(uuid4()), "tenant_id": tenant_id, "account_pool_id": pool_id,
        "revision": 1, "period_kind": "calendar_day", "rolling_window_days": 3,
        "required_activity_classes": ["any_confirmed_business_operation"],
        "class_targets": targets, "union_policy": "any_confirmed_business_operation",
        "classification_policy_revision": "fleet_activity_classification_v1",
        "state": "active", "effective_from": now_value,
    }


def _backfill_projection_states() -> None:
    bind = op.get_bind()
    facts = sa.table(
        "fulfillment_remote_facts",
        sa.column("fact_id", sa.String()),
        sa.column("fact_kind", sa.String()),
    )
    states = sa.table(
        "fulfillment_fact_projection_states",
        sa.column("id", sa.String()), sa.column("fact_id", sa.String()),
        sa.column("projection_kind", sa.String()),
        sa.column("expected_target_version", sa.Integer()),
        sa.column("state", sa.String()),
        sa.column("next_retry_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    eligible = bind.execute(sa.select(facts.c.fact_id).where(
        facts.c.fact_kind.in_((
            "view_observed", "reaction_observed", "remote_message_observed",
        ))
    )).scalars()
    now_value = datetime.now(timezone.utc)
    for fact_id in eligible:
        bind.execute(states.insert().values(
            id=str(uuid4()), fact_id=fact_id, projection_kind="fleet_activity",
            expected_target_version=0, state="pending",
            next_retry_at=now_value, updated_at=now_value,
        ))
def downgrade() -> None:
    op.drop_index("ix_fleet_activity_projection_timeline", table_name="account_fleet_activity_fact_projections")
    op.drop_table("account_fleet_activity_fact_projections")
    op.drop_index("ix_fleet_activity_pool_period", table_name="account_fleet_activity_ledgers")
    op.drop_table("account_fleet_activity_ledgers")
    op.drop_index("uq_fleet_activity_policy_active", table_name="account_fleet_activity_policy_revisions")
    op.drop_table("account_fleet_activity_policy_revisions")
