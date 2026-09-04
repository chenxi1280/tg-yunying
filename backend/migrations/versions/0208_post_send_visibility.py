"""Add unified post-send visibility policy and observation.

Revision ID: 0208_post_send_visibility
Revises: 0207_engagement_opportunity
"""

from alembic import op
import sqlalchemy as sa


revision = "0208_post_send_visibility"
down_revision = "0207_engagement_opportunity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "post_send_visibility_policy_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "normal_window_seconds", sa.Integer(), nullable=False, server_default="15"
        ),
        sa.Column(
            "elevated_window_seconds", sa.Integer(), nullable=False, server_default="90"
        ),
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id",
            "revision",
            name="uq_post_send_visibility_policy_revision",
        ),
    )
    op.create_index(
        "uq_post_send_visibility_policy_active",
        "post_send_visibility_policy_revisions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO post_send_visibility_policy_revisions (
                id, tenant_id, revision, normal_window_seconds,
                elevated_window_seconds, state, created_at
            )
            SELECT md5('post-send-visibility:' || id::text), id, 1, 15, 90,
                   'active', CURRENT_TIMESTAMP
            FROM tenants
            """
        )
    )
    op.create_table(
        "post_send_visibility_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Integer(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "policy_revision_id",
            sa.String(36),
            sa.ForeignKey("post_send_visibility_policy_revisions.id"),
            nullable=False,
        ),
        sa.Column(
            "action_id",
            sa.String(36),
            sa.ForeignKey("actions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "attempt_id",
            sa.String(36),
            sa.ForeignKey("execution_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("remote_message_id", sa.String(160), nullable=False),
        sa.Column("target_peer", sa.String(160), nullable=False),
        sa.Column("accepted_content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("state", sa.String(40), nullable=False, server_default="visibility_pending"),
        sa.Column("observer_route", sa.String(80), nullable=False, server_default="gateway_get_messages"),
        sa.Column("observer_watermark", sa.String(160), nullable=False, server_default=""),
        sa.Column("observer_gap", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_reason", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "action_id",
            "attempt_id",
            "policy_revision_id",
            name="uq_post_send_visibility_action_attempt_policy",
        ),
    )
    op.create_index(
        "ix_post_send_visibility_due",
        "post_send_visibility_observations",
        ["state", "deadline_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_post_send_visibility_due",
        table_name="post_send_visibility_observations",
    )
    op.drop_table("post_send_visibility_observations")
    op.drop_index(
        "uq_post_send_visibility_policy_active",
        table_name="post_send_visibility_policy_revisions",
    )
    op.drop_table("post_send_visibility_policy_revisions")
