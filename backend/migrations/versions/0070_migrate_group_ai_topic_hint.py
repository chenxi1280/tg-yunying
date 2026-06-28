"""migrate group ai topic hint

Revision ID: 0070_migrate_group_ai_topic_hint
Revises: 0069_expand_admin_chat_ids
Create Date: 2026-06-28 16:20:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0070_migrate_group_ai_topic_hint"
down_revision = "0069_expand_admin_chat_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("0070_migrate_group_ai_topic_hint requires PostgreSQL JSONB")
    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET type_config = jsonb_set(
                type_config::jsonb - 'topic_hint',
                '{topic_directions}',
                jsonb_build_array(
                    jsonb_build_object(
                        'title', btrim(type_config->>'topic_hint'),
                        'description', '',
                        'weight', 1.0
                    )
                ),
                true
            )::json
            WHERE type = 'group_ai_chat'
              AND type_config::jsonb ? 'topic_hint'
              AND btrim(coalesce(type_config->>'topic_hint', '')) <> ''
              AND (
                  NOT (type_config::jsonb ? 'topic_directions')
                  OR jsonb_typeof(type_config::jsonb->'topic_directions') <> 'array'
                  OR jsonb_array_length(type_config::jsonb->'topic_directions') = 0
              )
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE tasks
            SET type_config = (type_config::jsonb - 'topic_hint')::json
            WHERE type = 'group_ai_chat'
              AND type_config::jsonb ? 'topic_hint'
            """
        )
    )


def downgrade() -> None:
    return
