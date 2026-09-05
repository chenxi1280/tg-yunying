"""Keep retired Tasks terminal and retain their replacement identity."""
from alembic import op
import sqlalchemy as sa


revision = "0226_task_retirement"
down_revision = "0225_account_group_revisions"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("replaced_by_task_id", sa.String(36), nullable=True))
        batch.create_foreign_key("fk_tasks_retirement_replacement", "tasks",
            ["replaced_by_task_id"], ["id"], ondelete="RESTRICT")
        batch.create_unique_constraint("uq_tasks_retirement_replacement", ["replaced_by_task_id"])
        batch.create_check_constraint("ck_tasks_retirement_terminal",
            "(retired_at IS NULL AND replaced_by_task_id IS NULL) OR "
            "(retired_at IS NOT NULL AND replaced_by_task_id IS NOT NULL "
            "AND replaced_by_task_id != id AND status = 'stopped' AND next_run_at IS NULL)")


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(sa.text("LOCK TABLE tasks IN ACCESS EXCLUSIVE MODE"))
    if connection.scalar(sa.text("SELECT 1 FROM tasks WHERE retired_at IS NOT NULL LIMIT 1")):
        raise RuntimeError("task_retirement_downgrade_requires_empty_evidence")
    with op.batch_alter_table("tasks") as batch:
        batch.drop_constraint("ck_tasks_retirement_terminal", type_="check")
        batch.drop_constraint("uq_tasks_retirement_replacement", type_="unique")
        batch.drop_constraint("fk_tasks_retirement_replacement", type_="foreignkey")
        batch.drop_column("replaced_by_task_id")
        batch.drop_column("retired_at")
