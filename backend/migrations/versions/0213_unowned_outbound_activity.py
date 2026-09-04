"""Add unowned outbound observation and external-use holds.

Revision ID: 0213_unowned_outbound_activity
Revises: 0212_remote_fence_transport_ack
"""

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy import table, column


revision = "0213_unowned_outbound_activity"
down_revision = "0212_remote_fence_transport_ack"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_policy_table()
    _backfill_policies()
    _create_observation_table()
    _create_hold_table()


def _create_policy_table() -> None:
    name = "external_account_use_policy_revisions"
    op.create_table(
        name,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("hold_seconds_by_class", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("collision_classes_by_class", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "revision", name="uq_external_account_use_policy_revision"),
    )
    op.create_index(
        "uq_external_account_use_policy_active",
        name,
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def _backfill_policies() -> None:
    policies = table(
        "external_account_use_policy_revisions",
        column("id", sa.String), column("tenant_id", sa.Integer),
        column("revision", sa.Integer), column("hold_seconds_by_class", sa.JSON),
        column("collision_classes_by_class", sa.JSON), column("state", sa.String),
        column("created_at", sa.DateTime(timezone=True)),
    )
    tenants = table("tenants", column("id", sa.Integer))
    rows = op.get_bind().execute(sa.select(tenants.c.id)).all()
    op.bulk_insert(policies, [_policy_values(int(row.id)) for row in rows])


def _policy_values(tenant_id: int) -> dict:
    return {
        "id": str(uuid4()),
        "tenant_id": tenant_id,
        "revision": 1,
        "hold_seconds_by_class": {
            "authored_message": 600, "authored_comment": 600, "reaction": 300,
        },
        "collision_classes_by_class": {
            "authored_message": ["authored_message", "reaction"],
            "authored_comment": ["authored_comment", "reaction"],
            "reaction": ["authored_message", "authored_comment", "reaction"],
        },
        "state": "active",
        "created_at": datetime.now(timezone.utc),
    }


def _create_observation_table() -> None:
    observation = "unowned_outbound_activity_observations"
    op.create_table(
        observation,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("activity_class", sa.String(40), nullable=False),
        sa.Column("canonical_peer_id", sa.String(160), nullable=False),
        sa.Column("canonical_source_identity", sa.String(200), nullable=False, server_default=""),
        sa.Column("remote_identity", sa.String(160), nullable=False),
        sa.Column("activity_identity_hash", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(40), nullable=False, server_default="telegram_update"),
        sa.Column("source_event_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("ownership_state", sa.String(32), nullable=False, server_default="unowned"),
        sa.Column("ownership_evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("activity_identity_hash", name="uq_unowned_outbound_activity_identity"),
    )
    op.create_index(
        "ix_unowned_outbound_account_time",
        observation,
        ["tenant_id", "account_id", "observed_at"],
    )


def _create_hold_table() -> None:
    observation = "unowned_outbound_activity_observations"
    hold = "account_external_use_holds"
    op.create_table(
        hold,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observation_id", sa.String(36), sa.ForeignKey(f"{observation}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("policy_revision_id", sa.String(36), sa.ForeignKey("external_account_use_policy_revisions.id"), nullable=False),
        sa.Column("canonical_peer_id", sa.String(160), nullable=False),
        sa.Column("canonical_source_identity", sa.String(200), nullable=False, server_default=""),
        sa.Column("action_class", sa.String(40), nullable=False),
        sa.Column("collision_action_classes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("reason_code", sa.String(80), nullable=False, server_default="unowned_outbound_activity"),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("observation_id", name="uq_account_external_use_hold_observation"),
    )
    op.create_index(
        "ix_account_external_use_hold_active",
        hold,
        ["tenant_id", "account_id", "action_class", "state", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_account_external_use_hold_active",
        table_name="account_external_use_holds",
    )
    op.drop_table("account_external_use_holds")
    op.drop_index(
        "ix_unowned_outbound_account_time",
        table_name="unowned_outbound_activity_observations",
    )
    op.drop_table("unowned_outbound_activity_observations")
    op.drop_index(
        "uq_external_account_use_policy_active",
        table_name="external_account_use_policy_revisions",
    )
    op.drop_table("external_account_use_policy_revisions")
