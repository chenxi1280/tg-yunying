"""Treat masked phone values as display-only data.

Revision ID: 0171_phone_mask_display_idx
Revises: 0170_channel_reaction_cap
"""

from alembic import op
import sqlalchemy as sa


revision = "0171_phone_mask_display_idx"
down_revision = "0170_channel_reaction_cap"
branch_labels = None
depends_on = None


TABLE = "tg_accounts"
OLD_INDEX = "ux_tg_accounts_tenant_phone_active"
NEW_INDEX = "ix_tg_accounts_tenant_phone_masked_active"


def _index_names() -> set[str]:
    return {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(TABLE)}


def upgrade() -> None:
    indexes = _index_names()
    if OLD_INDEX in indexes:
        op.drop_index(OLD_INDEX, table_name=TABLE)
    if NEW_INDEX not in indexes:
        op.create_index(
            NEW_INDEX,
            TABLE,
            ["tenant_id", "phone_masked"],
            unique=False,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )


def downgrade() -> None:
    indexes = _index_names()
    if _has_active_mask_collision():
        raise RuntimeError("cannot restore masked-phone uniqueness while active collisions exist")
    if NEW_INDEX in indexes:
        op.drop_index(NEW_INDEX, table_name=TABLE)
    if OLD_INDEX not in indexes:
        op.create_index(
            OLD_INDEX,
            TABLE,
            ["tenant_id", "phone_masked"],
            unique=True,
            postgresql_where=sa.text("deleted_at IS NULL"),
        )


def _has_active_mask_collision() -> bool:
    row = op.get_bind().execute(sa.text(
        "SELECT 1 FROM tg_accounts WHERE deleted_at IS NULL "
        "GROUP BY tenant_id, phone_masked HAVING COUNT(*) > 1 LIMIT 1"
    )).first()
    return row is not None
