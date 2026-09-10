"""Share immutable account paths while preserving admission snapshot identities."""
from alembic import op
import sqlalchemy as sa

revision = "0232_admission_evidence"
down_revision = "0231_ai_group_emergency_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planning_admission_evidence",
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("digest", sa.String(64), primary_key=True),
        sa.Column("account_paths", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("planning_admission_snapshots", sa.Column("account_paths_digest", sa.String(64), nullable=True))
    op.create_foreign_key("fk_planning_admission_evidence", "planning_admission_snapshots",
                          "planning_admission_evidence", ["tenant_id", "account_paths_digest"], ["tenant_id", "digest"])
    op.create_index("ix_planning_admission_evidence", "planning_admission_snapshots", ["tenant_id", "account_paths_digest"])


def downgrade() -> None:
    references = op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM planning_admission_snapshots WHERE account_paths_digest IS NOT NULL)",
    ))
    if references:
        raise RuntimeError("Shared admission evidence requires a compatible forward fix")
    op.drop_index("ix_planning_admission_evidence", table_name="planning_admission_snapshots")
    op.drop_constraint("fk_planning_admission_evidence", "planning_admission_snapshots", type_="foreignkey")
    op.drop_column("planning_admission_snapshots", "account_paths_digest")
    op.drop_table("planning_admission_evidence")
