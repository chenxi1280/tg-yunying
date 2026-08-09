"""Add tenant-scoped account profile name claims."""

import re
import unicodedata

from alembic import op
import sqlalchemy as sa


revision = "0143_account_profile_name_claims"
down_revision = "0142_ai_context_recent_index"
branch_labels = None
depends_on = None


TABLE_NAME = "tg_account_profile_name_claims"
SPACE_RE = re.compile(r"\s+")
ZERO_WIDTH_CHARACTERS = frozenset({"\u200b", "\u200c", "\u200d", "\ufeff"})


def upgrade() -> None:
    if TABLE_NAME in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE_NAME,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("name_key", sa.String(length=160), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("tg_account_security_batches.id"), nullable=True),
        sa.Column("batch_item_id", sa.Integer(), sa.ForeignKey("tg_account_security_batch_items.id"), nullable=True),
        sa.Column("trace_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "name_key", name="uq_account_profile_name_claim_tenant_key"),
        sa.UniqueConstraint("tenant_id", "account_id", "name_key", name="uq_account_profile_name_claim_idempotency"),
    )
    _backfill_current_name_keepers()


def downgrade() -> None:
    op.drop_table(TABLE_NAME)


def _backfill_current_name_keepers() -> None:
    rows = op.get_bind().execute(sa.text("""
        SELECT tenant_id, id, display_name
        FROM tg_accounts
        WHERE deleted_at IS NULL
          AND account_identity = 'normal'
          AND trim(display_name) <> ''
        ORDER BY tenant_id,
                 CASE WHEN profile_sync_status = '已同步' THEN 0 ELSE 1 END,
                 CASE WHEN avatar_object_key <> '' THEN 0 ELSE 1 END,
                 created_at,
                 id
    """)).mappings()
    keepers: dict[tuple[int, str], dict[str, object]] = {}
    for row in rows:
        key = _normalize_display_name(str(row["display_name"]))
        if key:
            keepers.setdefault((int(row["tenant_id"]), key), dict(row))
    payloads = [
        {
            "tenant_id": tenant_id,
            "account_id": int(row["id"]),
            "display_name": str(row["display_name"]).strip(),
            "name_key": name_key,
        }
        for (tenant_id, name_key), row in keepers.items()
    ]
    if payloads:
        op.get_bind().execute(sa.text("""
            INSERT INTO tg_account_profile_name_claims
                (tenant_id, account_id, display_name, name_key, source, trace_id, created_by, created_at)
            VALUES
                (:tenant_id, :account_id, :display_name, :name_key, 'migration_backfill', '', 'alembic', CURRENT_TIMESTAMP)
        """), payloads)


def _normalize_display_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    visible = "".join(char for char in normalized if char not in ZERO_WIDTH_CHARACTERS)
    return SPACE_RE.sub(" ", visible).strip().casefold()
