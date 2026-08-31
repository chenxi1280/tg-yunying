"""Add explicit material membership to material groups.

Revision ID: 0186_material_group_members
Revises: 0185_channel_comment_fallback

The initial migration builds current metadata on a fresh database, so this
migration must not recreate columns that already exist in that path.
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0186_material_group_members"
down_revision = "0185_channel_comment_fallback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if _has_column("material_groups", "material_ids"):
        return
    op.add_column(
        "material_groups",
        sa.Column("material_ids", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.execute(
        """
        UPDATE material_groups AS target
        SET material_ids = matched.material_ids
        FROM (
            SELECT groups.id,
                   COALESCE(json_agg(materials.id ORDER BY materials.id)
                            FILTER (WHERE materials.id IS NOT NULL), '[]'::json)
                       AS material_ids
            FROM material_groups AS groups
            LEFT JOIN materials
              ON materials.tenant_id = groups.tenant_id
             AND materials.material_type = groups.group_type
            WHERE NOT EXISTS (
                SELECT 1 FROM material_groups AS sibling
                WHERE sibling.tenant_id = groups.tenant_id
                  AND sibling.group_type = groups.group_type
                  AND sibling.id <> groups.id
            )
            GROUP BY groups.id
        ) AS matched
        WHERE target.id = matched.id
        """
    )
    op.alter_column("material_groups", "material_ids", server_default=None)


def _has_column(table_name: str, column_name: str) -> bool:
    if context.is_offline_mode():
        return False
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return any(column["name"] == column_name for column in columns)


def downgrade() -> None:
    op.drop_column("material_groups", "material_ids")
