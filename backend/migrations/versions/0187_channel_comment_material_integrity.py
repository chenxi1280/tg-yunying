"""Close channel comment material-group and gateway-fact integrity gaps.

Revision ID: 0187_comment_material_integrity
Revises: 0186_material_group_members

The initial migration builds current metadata on a fresh database, so this
migration must not recreate columns that already exist in that path.
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0187_comment_material_integrity"
down_revision = "0186_material_group_members"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if _has_column("material_groups", "membership_revision"):
        return
    op.add_column(
        "material_groups",
        sa.Column("membership_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "material_groups",
        sa.Column("membership_state", sa.String(length=24), nullable=False, server_default="ready"),
    )
    op.add_column(
        "material_groups",
        sa.Column("membership_state_reason", sa.String(length=160), nullable=False, server_default=""),
    )
    op.execute(
        """
        UPDATE material_groups AS target
        SET membership_state = 'review_required',
            membership_state_reason = 'ambiguous_same_type_groups'
        WHERE json_array_length(target.material_ids) = 0
          AND EXISTS (
              SELECT 1 FROM material_groups AS sibling
              WHERE sibling.tenant_id = target.tenant_id
                AND sibling.group_type = target.group_type
                AND sibling.id <> target.id
          )
        """
    )
    op.add_column(
        "gateway_request_evidence_journals",
        sa.Column("typed_remote_fact", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.alter_column("material_groups", "membership_revision", server_default=None)
    op.alter_column("material_groups", "membership_state", server_default=None)
    op.alter_column("material_groups", "membership_state_reason", server_default=None)
    op.alter_column("gateway_request_evidence_journals", "typed_remote_fact", server_default=None)


def _has_column(table_name: str, column_name: str) -> bool:
    if context.is_offline_mode():
        return False
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def downgrade() -> None:
    op.drop_column("gateway_request_evidence_journals", "typed_remote_fact")
    op.drop_column("material_groups", "membership_state_reason")
    op.drop_column("material_groups", "membership_state")
    op.drop_column("material_groups", "membership_revision")
