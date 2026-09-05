"""Add original account-group membership and state revision evidence."""
from alembic import op
import sqlalchemy as sa


revision = "0225_account_group_revisions"
down_revision = "0224_legacy_account_occupancy"
branch_labels = None
depends_on = None

MEMBERSHIP_TABLE = "account_group_membership_revisions"
STATE_TABLE = "account_group_state_revisions"


def _identity_columns(table):
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_pool_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_revision_id", sa.String(36), sa.ForeignKey(f"{table}.id"), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("reason", sa.String(160), nullable=False),
    ]


def upgrade():
    op.create_table(MEMBERSHIP_TABLE, *_identity_columns(MEMBERSHIP_TABLE),
        sa.Column("member_account_ids", sa.JSON(), nullable=False),
        sa.Column("member_contracts", sa.JSON(), nullable=False),
        sa.Column("member_set_hash", sa.String(64), nullable=False),
        sa.Column("membership_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("tenant_id", "account_pool_id", "revision",
            name="uq_account_group_membership_revision"))
    op.create_table(STATE_TABLE, *_identity_columns(STATE_TABLE),
        sa.Column("group_state", sa.JSON(), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("tenant_id", "account_pool_id", "revision",
            name="uq_account_group_state_revision"))


def downgrade():
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(sa.text("LOCK TABLE account_group_membership_revisions, "
            "account_group_state_revisions IN ACCESS EXCLUSIVE MODE"))
    for name in (MEMBERSHIP_TABLE, STATE_TABLE):
        table = sa.table(name, sa.column("id"))
        if connection.scalar(sa.select(sa.literal(1)).select_from(table).limit(1)):
            raise RuntimeError("account_group_revision_downgrade_requires_empty_evidence")
    op.drop_table(STATE_TABLE)
    op.drop_table(MEMBERSHIP_TABLE)
