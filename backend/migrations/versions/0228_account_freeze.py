"""Persist Telegram account freeze observations independently of connectivity."""
from alembic import op
import sqlalchemy as sa

revision = "0228_account_freeze"
down_revision = "0227_ai_group_history_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tg_accounts", sa.Column("telegram_frozen", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("tg_accounts", sa.Column("telegram_freeze_observed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Account freeze observations must be preserved; deploy a compatible forward fix")
