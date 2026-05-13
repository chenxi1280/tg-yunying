"""risk control proxy center

Revision ID: 0032_risk_control_proxy_center
Revises: 0031_rule_center_refactor_fields
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0032_risk_control_proxy_center"
down_revision = "0031_rule_center_refactor_fields"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _table_exists(name: str) -> bool:
    return sa.inspect(_bind()).has_table(name)


def _column_names(table: str) -> set[str]:
    if not _table_exists(table):
        return set()
    return {item["name"] for item in sa.inspect(_bind()).get_columns(table)}


def upgrade() -> None:
    if not _table_exists("account_proxies"):
        op.create_table(
            "account_proxies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("protocol", sa.String(16), nullable=False, server_default="socks5"),
            sa.Column("host", sa.String(120), nullable=False, server_default="127.0.0.1"),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("username", sa.String(120), nullable=False, server_default=""),
            sa.Column("password_ciphertext", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(30), nullable=False, server_default="unknown"),
            sa.Column("alert_status", sa.String(30), nullable=False, server_default="normal"),
            sa.Column("check_interval_seconds", sa.Integer(), nullable=False, server_default="300"),
            sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="3000"),
            sa.Column("max_bound_accounts", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("max_concurrent_sessions", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("last_check_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("disabled_reason", sa.String(255), nullable=False, server_default=""),
            sa.Column("notes", sa.String(255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("tenant_id", "name", name="uq_account_proxies_tenant_name"),
        )
    if "proxy_id" not in _column_names("tg_accounts"):
        op.add_column("tg_accounts", sa.Column("proxy_id", sa.Integer(), nullable=True))
    if not _table_exists("account_proxy_bindings"):
        op.create_table(
            "account_proxy_bindings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("proxy_id", sa.Integer(), sa.ForeignKey("account_proxies.id"), nullable=True),
            sa.Column("status", sa.String(30), nullable=False, server_default="active"),
            sa.Column("change_reason", sa.String(255), nullable=False, server_default=""),
            sa.Column("bound_by", sa.String(100), nullable=False, server_default=""),
            sa.Column("bound_at", sa.DateTime(), nullable=False),
            sa.Column("unbound_at", sa.DateTime(), nullable=True),
        )
    if not _table_exists("proxy_alerts"):
        op.create_table(
            "proxy_alerts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("proxy_id", sa.Integer(), sa.ForeignKey("account_proxies.id"), nullable=False),
            sa.Column("severity", sa.String(20), nullable=False, server_default="warning"),
            sa.Column("status", sa.String(30), nullable=False, server_default="alerting"),
            sa.Column("alert_type", sa.String(60), nullable=False, server_default="manual"),
            sa.Column("reason_code", sa.String(80), nullable=False, server_default=""),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("recovered_at", sa.DateTime(), nullable=True),
            sa.Column("acknowledged_by", sa.String(100), nullable=False, server_default=""),
            sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
            sa.Column("ignored_until", sa.DateTime(), nullable=True),
            sa.Column("affected_account_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("related_risk_event_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("suggested_action", sa.String(255), nullable=False, server_default=""),
            sa.Column("audit_id", sa.String(80), nullable=False, server_default=""),
        )
    if not _table_exists("proxy_health_checks"):
        op.create_table(
            "proxy_health_checks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("proxy_id", sa.Integer(), sa.ForeignKey("account_proxies.id"), nullable=False),
            sa.Column("check_type", sa.String(40), nullable=False, server_default="port_connect"),
            sa.Column("status", sa.String(30), nullable=False, server_default="unknown"),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("error_code", sa.String(80), nullable=False, server_default=""),
            sa.Column("error_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("checked_by", sa.String(100), nullable=False, server_default=""),
            sa.Column("checked_at", sa.DateTime(), nullable=False),
            sa.Column("trace_id", sa.String(80), nullable=False, server_default=""),
        )


def downgrade() -> None:
    for table in ["proxy_health_checks", "proxy_alerts", "account_proxy_bindings", "account_proxies"]:
        if _table_exists(table):
            op.drop_table(table)
    if "proxy_id" in _column_names("tg_accounts"):
        op.drop_column("tg_accounts", "proxy_id")
