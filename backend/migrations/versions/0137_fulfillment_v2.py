"""Install the fact-first fulfillment v2 contract.

Revision ID: 0137_fulfillment_v2
Revises: 0136_ai_gen_group_occupancy
Create Date: 2026-08-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.database import Base
from app import models  # noqa: F401


revision = "0137_fulfillment_v2"
down_revision = "0136_ai_gen_group_occupancy"
branch_labels = None
depends_on = None

NEW_TABLES = (
    "fulfillment_obligation_projections",
    "fulfillment_remote_facts",
    "fulfillment_fact_projection_states",
    "generation_jobs",
    "search_click_assignments",
    "search_protocol_sessions",
    "recoverable_work_leases",
    "task_group_bot_admissions",
    "account_group_admission_facts",
    "task_contract_activation_manifests",
    "task_contract_routes",
    "task_delete_operations",
    "task_delete_operation_items",
    "remote_mutation_tombstones",
)

MODEL_COLUMNS = {
    "tasks": (
        "task_lifecycle_epoch",
        "fulfillment_contract_version",
        "group_ai_prejoin_channel_ids",
    ),
    "actions": (
        "execution_lane",
        "obligation_type",
        "obligation_id",
        "materialization_version",
        "action_version",
        "task_lifecycle_epoch",
        "unknown_deadline_at",
    ),
    "execution_attempts": (
        "task_lifecycle_epoch",
    ),
    "task_group_daily_targets": (
        "planned_target_revision",
        "planned_daily_target",
        "gateway_started_count",
        "unknown_hold_count",
        "target_reduction_overage_count",
        "target_changed_at",
        "target_change_reason",
    ),
    "remote_reconcile_cases": (
        "next_probe_at",
        "unknown_deadline_at",
    ),
    "tg_account_authorizations": (
        "fact_version",
        "last_authoritative_error_code",
        "last_authoritative_observed_at",
    ),
}

SERVER_DEFAULTS = {
    ("tasks", "task_lifecycle_epoch"): "1",
    ("tasks", "fulfillment_contract_version"): "'legacy_v1'",
    ("tasks", "group_ai_prejoin_channel_ids"): "'[]'",
    ("actions", "execution_lane"): "'interaction'",
    ("actions", "materialization_version"): "1",
    ("actions", "action_version"): "1",
    ("actions", "task_lifecycle_epoch"): "1",
    ("execution_attempts", "task_lifecycle_epoch"): "1",
    ("task_group_daily_targets", "planned_target_revision"): "1",
    ("task_group_daily_targets", "planned_daily_target"): "1",
    ("task_group_daily_targets", "gateway_started_count"): "0",
    ("task_group_daily_targets", "unknown_hold_count"): "0",
    ("task_group_daily_targets", "target_reduction_overage_count"): "0",
    ("task_group_daily_targets", "target_change_reason"): "'created'",
    ("tg_account_authorizations", "fact_version"): "1",
    ("tg_account_authorizations", "last_authoritative_error_code"): "''",
}


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in NEW_TABLES:
        Base.metadata.tables[table_name].create(bind, checkfirst=True)
    for table_name, column_names in MODEL_COLUMNS.items():
        for column_name in column_names:
            _add_model_column(table_name, column_name)
    _drop_index_if_exists("actions", "uq_actions_executing_account")
    _create_action_indexes()
    _create_remote_reconcile_index()
    _cascade_runtime_delete_foreign_keys()


def _add_model_column(table_name: str, column_name: str) -> None:
    if column_name in _column_names(table_name):
        return
    model_column = Base.metadata.tables[table_name].c[column_name]
    default = SERVER_DEFAULTS.get((table_name, column_name))
    op.add_column(
        table_name,
        sa.Column(
            column_name,
            model_column.type,
            nullable=model_column.nullable,
            server_default=sa.text(default) if default is not None else None,
        ),
    )


def _create_action_indexes() -> None:
    names = _index_names("actions")
    if "uq_actions_open_obligation" not in names:
        op.create_index(
            "uq_actions_open_obligation",
            "actions",
            ["obligation_type", "obligation_id"],
            unique=True,
            postgresql_where=sa.text(
                "obligation_id IS NOT NULL AND status IN "
                "('pending','claiming','executing','unknown_after_send')"
            ),
            sqlite_where=sa.text(
                "obligation_id IS NOT NULL AND status IN "
                "('pending','claiming','executing','unknown_after_send')"
            ),
        )
    if "ix_actions_lane_claim_ready" not in names:
        op.create_index(
            "ix_actions_lane_claim_ready",
            "actions",
            ["tenant_id", "execution_lane", "scheduled_at", "task_id", "id"],
            postgresql_where=sa.text("status = 'pending'"),
            sqlite_where=sa.text("status = 'pending'"),
        )


def _create_remote_reconcile_index() -> None:
    if "ix_remote_reconcile_due" in _index_names("remote_reconcile_cases"):
        return
    op.create_index(
        "ix_remote_reconcile_due",
        "remote_reconcile_cases",
        ["next_probe_at", "id"],
        postgresql_where=sa.text("state = 'open'"),
        sqlite_where=sa.text("state = 'open'"),
    )


def _drop_index_if_exists(table_name: str, index_name: str) -> None:
    if index_name in _index_names(table_name):
        op.drop_index(index_name, table_name=table_name)


def _cascade_runtime_delete_foreign_keys() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    for table_name in inspector.get_table_names():
        for foreign_key in inspector.get_foreign_keys(table_name):
            if foreign_key.get("referred_table") not in {
                "tasks",
                "actions",
                "execution_attempts",
            }:
                continue
            ondelete = ((foreign_key.get("options") or {}).get("ondelete") or "").upper()
            if ondelete:
                continue
            _replace_runtime_foreign_key(table_name, foreign_key)


def _replace_runtime_foreign_key(table_name: str, foreign_key: dict) -> None:
    constraint_name = foreign_key.get("name")
    if not constraint_name:
        raise RuntimeError(f"task foreign key is unnamed: {table_name}")
    op.drop_constraint(constraint_name, table_name, type_="foreignkey")
    op.create_foreign_key(
        constraint_name,
        table_name,
        foreign_key["referred_table"],
        foreign_key["constrained_columns"],
        foreign_key["referred_columns"],
        ondelete="CASCADE",
    )


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _index_names(table_name: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table_name)}


def downgrade() -> None:
    raise RuntimeError("0137 fulfillment v2 is forward-only")
