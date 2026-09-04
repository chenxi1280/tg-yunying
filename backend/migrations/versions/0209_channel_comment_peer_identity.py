"""Scope channel comment identity to the linked discussion peer.

Revision ID: 0209_comment_peer_identity
Revises: 0208_post_send_visibility
"""

from alembic import op
import sqlalchemy as sa


revision = "0209_comment_peer_identity"
down_revision = "0208_post_send_visibility"
branch_labels = None
depends_on = None

OLD_COLUMNS = {
    "tenant_id",
    "channel_target_id",
    "channel_message_id",
    "comment_message_id",
}
NEW_CONSTRAINT = "uq_channel_message_comment_peer_identity"


def upgrade() -> None:
    op.add_column(
        "channel_message_comments",
        sa.Column(
            "discussion_peer_id",
            sa.String(length=160),
            nullable=False,
            server_default="",
        ),
    )
    _backfill_current_discussion_peers(op.get_bind())
    old_name = _old_unique_constraint_name()
    if not old_name:
        raise RuntimeError("channel_message_comment_legacy_unique_constraint_missing")
    op.drop_constraint(old_name, "channel_message_comments", type_="unique")
    op.create_unique_constraint(
        NEW_CONSTRAINT,
        "channel_message_comments",
        [
            "tenant_id",
            "channel_target_id",
            "channel_message_id",
            "discussion_peer_id",
            "comment_message_id",
        ],
    )
    op.alter_column(
        "channel_message_comments", "discussion_peer_id", server_default=None,
    )


def downgrade() -> None:
    op.drop_constraint(NEW_CONSTRAINT, "channel_message_comments", type_="unique")
    op.create_unique_constraint(
        "uq_channel_message_comment_legacy_identity",
        "channel_message_comments",
        ["tenant_id", "channel_target_id", "channel_message_id", "comment_message_id"],
    )
    op.drop_column("channel_message_comments", "discussion_peer_id")


def _old_unique_constraint_name() -> str:
    constraints = sa.inspect(op.get_bind()).get_unique_constraints(
        "channel_message_comments"
    )
    return next(
        (
            str(item["name"])
            for item in constraints
            if item.get("name")
            and set(item.get("column_names") or []) == OLD_COLUMNS
        ),
        "",
    )


def _backfill_current_discussion_peers(bind) -> None:
    bind.execute(sa.text("""
        UPDATE channel_message_comments
        SET discussion_peer_id = COALESCE((
            SELECT binding.discussion_peer_id
            FROM channel_messages AS message
            JOIN channel_message_source_revisions AS revision
              ON revision.id = message.current_source_revision_id
            JOIN channel_discussion_thread_bindings AS binding
              ON binding.id = revision.discussion_thread_binding_id
             AND binding.source_revision_id = revision.id
             AND binding.is_current = true
            WHERE message.id = channel_message_comments.channel_message_id
              AND revision.tenant_id = channel_message_comments.tenant_id
              AND revision.channel_target_id = channel_message_comments.channel_target_id
              AND (
                  SELECT COUNT(DISTINCT historical_binding.discussion_peer_id)
                  FROM channel_message_source_revisions AS historical_revision
                  JOIN channel_discussion_thread_bindings AS historical_binding
                    ON historical_binding.source_revision_id = historical_revision.id
                  WHERE historical_revision.channel_message_id = message.id
              ) = 1
            LIMIT 1
        ), '')
        WHERE discussion_peer_id = ''
    """))
