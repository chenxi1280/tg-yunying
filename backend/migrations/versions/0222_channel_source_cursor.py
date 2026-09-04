"""Resume bounded channel source pages without losing the confirmed watermark."""
from alembic import op
import sqlalchemy as sa

revision = "0222_channel_source_cursor"
down_revision = "0221_album_reaction"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("channel_source_page_cursors",
        sa.Column("listener_source_state_id", sa.String(36),
            sa.ForeignKey("listener_source_state.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("page_state", sa.JSON(), nullable=False))


def downgrade():
    op.drop_table("channel_source_page_cursors")
