"""Remove historical approval prerequisites without erasing old evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0219_lightweight_timing"
down_revision = "0218_provider_lineage"
branch_labels = None
depends_on = None

HISTORICAL_COLUMNS = {
    "timing_profile_id": sa.String(36),
    "profile_snapshot_hash": sa.String(64),
    "resilience_policy_id": sa.String(36),
}


def upgrade() -> None:
    with op.batch_alter_table("generation_timing_bindings") as batch:
        for name, column_type in HISTORICAL_COLUMNS.items():
            batch.alter_column(name, existing_type=column_type, nullable=True)


def downgrade() -> None:
    table = sa.table("generation_timing_bindings", *(sa.column(name) for name in HISTORICAL_COLUMNS))
    missing = sa.or_(*(table.c[name].is_(None) for name in HISTORICAL_COLUMNS))
    if op.get_bind().scalar(sa.select(sa.literal(1)).select_from(table).where(missing).limit(1)):
        raise RuntimeError("lightweight_timing_downgrade_requires_historical_bindings")
    with op.batch_alter_table("generation_timing_bindings") as batch:
        for name, column_type in HISTORICAL_COLUMNS.items():
            batch.alter_column(name, existing_type=column_type, nullable=False)
