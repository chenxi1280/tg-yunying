"""Add shared dispatch recovery contracts and control facts.

Revision ID: 0134_shared_dispatch
Revises: 0133_group_listener_cursor
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.database import Base
from app import models  # noqa: F401


revision = "0134_shared_dispatch"
down_revision = "0133_group_listener_cursor"
branch_labels = None
depends_on = None

NEW_TABLES = (
    "dispatch_runtime_shard_states",
    "ai_content_scope_takeover_batches",
    "ai_content_scope_takeover_items",
    "remote_reconcile_cases",
    "gateway_request_evidence_journals",
)

MODEL_COLUMNS = {
    "dispatch_claim_scopes": {
        "runtime_shard_total": "0",
        "topology_fingerprint": "''",
        "capacity_config_fingerprint": "''",
        "fingerprint_schema_version": "''",
        "candidate_contract_version": "''",
        "active_contract_version": "''",
        "contract_activation_state": "'preparing'",
    },
    "dispatch_claim_windows": {
        "effective_unclaimed_count": "0",
    },
    "dispatch_claim_shard_allocations": {
        "dispatch_contract_version": "''",
    },
}


def upgrade() -> None:
    for table_name in NEW_TABLES:
        Base.metadata.tables[table_name].create(op.get_bind(), checkfirst=True)
    for table_name, columns in MODEL_COLUMNS.items():
        for column_name, default in columns.items():
            _add_model_column(table_name, column_name, default)


def _add_model_column(
    table_name: str,
    column_name: str,
    server_default: str,
) -> None:
    if _has_column(table_name, column_name):
        return
    model_column = Base.metadata.tables[table_name].c[column_name]
    op.add_column(
        table_name,
        sa.Column(
            column_name,
            model_column.type,
            nullable=False,
            server_default=sa.text(server_default),
        ),
    )


def _has_column(table_name: str, column_name: str) -> bool:
    columns = sa.inspect(op.get_bind()).get_columns(table_name)
    return column_name in {column["name"] for column in columns}


def downgrade() -> None:
    raise RuntimeError("0134 shared dispatch migration is forward-only")
