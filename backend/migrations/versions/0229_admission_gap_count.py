"""Keep observation failures separate from successful observation revisions."""
from alembic import op
import sqlalchemy as sa

revision = "0229_admission_gap_count"
down_revision = "0228_account_freeze"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_group_bot_admissions", sa.Column(
        "consecutive_observation_gaps", sa.Integer(), nullable=False, server_default="0",
    ))


def downgrade() -> None:
    recorded = op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM task_group_bot_admissions WHERE consecutive_observation_gaps <> 0",
    ))
    if recorded:
        raise RuntimeError("Observation failure evidence must be preserved; deploy a compatible forward fix")
    op.drop_column("task_group_bot_admissions", "consecutive_observation_gaps")
