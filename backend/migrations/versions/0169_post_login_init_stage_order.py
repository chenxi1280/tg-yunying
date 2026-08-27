"""Run profile and ABC preparation before waiting for Telegram 2FA reset.

Revision ID: 0169_post_login_stage_order
Revises: 0168_post_login_full_init
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0169_post_login_stage_order"
down_revision = "0168_post_login_full_init"
branch_labels = None
depends_on = None


TABLE = "tg_account_full_initializations"
TERMINAL_STATUSES = "('succeeded','failed','manual_required','reconcile_unknown','cancelled')"


def _column_names() -> set[str]:
    if context.is_offline_mode():
        return set()
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(TABLE)
    }


def _add_retry_column() -> None:
    if "two_fa_next_retry_at" in _column_names():
        return
    op.add_column(TABLE, sa.Column("two_fa_next_retry_at", sa.DateTime(), nullable=True))


def _backfill_reset_deadline() -> None:
    op.execute(sa.text(f"""
        UPDATE {TABLE}
        SET two_fa_next_retry_at = next_retry_at
        WHERE two_fa_status = 'reset_waiting'
          AND two_fa_next_retry_at IS NULL
          AND next_retry_at IS NOT NULL
    """))


def _route_active_reset_waiters() -> None:
    op.execute(sa.text(f"""
        UPDATE {TABLE}
        SET stage = CASE
                WHEN profile_status <> 'succeeded' OR profile_evidence_ref = '' THEN 'profile'
                WHEN abc_status NOT IN ('succeeded', 'waiting_prerequisite') THEN 'abc'
                ELSE 'two_fa'
            END,
            status = 'pending',
            next_retry_at = CASE
                WHEN profile_status <> 'succeeded' OR profile_evidence_ref = '' THEN NULL
                WHEN abc_status NOT IN ('succeeded', 'waiting_prerequisite') THEN NULL
                ELSE two_fa_next_retry_at
            END,
            lease_token = '',
            lease_expires_at = NULL,
            finished_at = NULL,
            version = version + 1
        WHERE two_fa_status = 'reset_waiting'
          AND status NOT IN {TERMINAL_STATUSES}
    """))


def upgrade() -> None:
    _add_retry_column()
    op.alter_column(TABLE, "stage", server_default="profile")
    _backfill_reset_deadline()
    _route_active_reset_waiters()


def downgrade() -> None:
    op.alter_column(TABLE, "stage", server_default="two_fa")
    if context.is_offline_mode() or "two_fa_next_retry_at" in _column_names():
        op.drop_column(TABLE, "two_fa_next_retry_at")
