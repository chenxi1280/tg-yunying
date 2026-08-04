"""Enforce one active AI Provider key."""

from alembic import op
import sqlalchemy as sa


revision = "0141_single_active_ai_provider"
down_revision = "0140_task_delete_recursive_idx"
branch_labels = None
depends_on = None


WINNER_SQL = """
SELECT p.id
FROM ai_providers AS p
LEFT JOIN tenant_ai_settings AS s ON s.default_provider_id = p.id
WHERE p.is_active = true
GROUP BY p.id
ORDER BY count(s.id) DESC, p.id
LIMIT 1
"""


def upgrade() -> None:
    op.execute(sa.text(f"""
        WITH winner AS ({WINNER_SQL})
        UPDATE tenant_ai_settings
        SET default_provider_id = winner.id
        FROM winner
        WHERE tenant_ai_settings.default_provider_id IS DISTINCT FROM winner.id
    """))
    op.execute(sa.text(f"""
        WITH winner AS ({WINNER_SQL})
        UPDATE ai_providers
        SET is_active = false,
            health_status = '禁用',
            updated_at = CURRENT_TIMESTAMP
        WHERE is_active = true
          AND id <> (SELECT id FROM winner)
    """))
    op.create_index(
        "uq_ai_provider_single_active",
        "ai_providers",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("uq_ai_provider_single_active", table_name="ai_providers", if_exists=True)
