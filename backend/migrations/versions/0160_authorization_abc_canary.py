"""Add A-fenced ABC authorization canary facts.

Revision ID: 0160_abc_canary
Revises: 0159_dr_repair
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0160_abc_canary"
down_revision = "0159_dr_repair"
branch_labels = None
depends_on = None


RUNTIME_COLUMNS = (
    sa.Column("required_node_capability_version", sa.String(80), nullable=False, server_default=""),
    sa.Column("required_node_runtime_image_sha", sa.String(64), nullable=False, server_default=""),
    sa.Column("claim_scope_operation_id", sa.String(36), nullable=False, server_default=""),
)

OPERATION_COLUMNS = (
    sa.Column("code_source_authorization_id", sa.Integer(), nullable=True),
    sa.Column("expected_current_authorization_id", sa.Integer(), nullable=True),
    sa.Column("expected_authorization_generation", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("expected_authorization_fact_generation", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("expected_connection_generation", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("expected_code_source_fact_version", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("expected_code_source_user_id_digest", sa.String(64), nullable=False, server_default=""),
    sa.Column("expected_code_source_auth_key_digest", sa.String(64), nullable=False, server_default=""),
    sa.Column("login_flow_id", sa.Integer(), nullable=True),
    sa.Column("login_challenge_sent_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("login_code_message_id", sa.String(64), nullable=False, server_default=""),
    sa.Column("login_code_received_at", sa.DateTime(timezone=True), nullable=True),
)


def upgrade() -> None:
    _add_missing("authorization_dr_runtime_contracts", RUNTIME_COLUMNS)
    _add_missing("tg_authorization_dr_operations", OPERATION_COLUMNS)
    _add_code_source_foreign_key()


def _add_missing(table_name: str, columns: tuple[sa.Column, ...]) -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table_name, column)


def _add_code_source_foreign_key() -> None:
    inspector = sa.inspect(op.get_bind())
    foreign_keys = {item.get("name") for item in inspector.get_foreign_keys("tg_authorization_dr_operations")}
    name = "fk_dr_operation_code_source_authorization"
    if name not in foreign_keys:
        op.create_foreign_key(
            name,
            "tg_authorization_dr_operations",
            "tg_account_authorizations",
            ["code_source_authorization_id"],
            ["id"],
        )


def downgrade() -> None:
    op.drop_constraint(
        "fk_dr_operation_code_source_authorization",
        "tg_authorization_dr_operations",
        type_="foreignkey",
    )
    for column in reversed(OPERATION_COLUMNS):
        op.drop_column("tg_authorization_dr_operations", column.name)
    for column in reversed(RUNTIME_COLUMNS):
        op.drop_column("authorization_dr_runtime_contracts", column.name)
