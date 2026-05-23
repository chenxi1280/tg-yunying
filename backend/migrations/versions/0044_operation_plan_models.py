"""operation plan models

Revision ID: 0044_operation_plan_models
Revises: 0043_runtime_summary_models
Create Date: 2026-05-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0044_operation_plan_models"
down_revision = "0043_runtime_summary_models"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _tables() -> set[str]:
    return set(sa.inspect(_bind()).get_table_names())


def _has_table(name: str) -> bool:
    return name in _tables()


def upgrade() -> None:
    if not _has_table("operation_plan_templates"):
        op.create_table(
            "operation_plan_templates",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("target_type", sa.String(length=30), nullable=False, server_default="group"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
            sa.Column("strategy_config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("task_blueprints", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("created_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("updated_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_operation_plan_templates_status", "operation_plan_templates", ["tenant_id", "status"])

    if not _has_table("operation_plan_targets"):
        op.create_table(
            "operation_plan_targets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("operation_plan_templates.id"), nullable=False),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
            sa.Column("strategy_config", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_unique_constraint("uq_operation_plan_targets_target", "operation_plan_targets", ["tenant_id", "plan_id", "target_id"])
        op.create_index("ix_operation_plan_targets_plan", "operation_plan_targets", ["tenant_id", "plan_id", "status"])

    if not _has_table("operation_plan_task_links"):
        op.create_table(
            "operation_plan_task_links",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("operation_plan_templates.id"), nullable=False),
            sa.Column("target_id", sa.Integer(), sa.ForeignKey("operation_targets.id"), nullable=True),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("relation", sa.String(length=40), nullable=False, server_default="generated"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_unique_constraint("uq_operation_plan_task_links_task", "operation_plan_task_links", ["tenant_id", "plan_id", "task_id"])
        op.create_index("ix_operation_plan_task_links_plan", "operation_plan_task_links", ["tenant_id", "plan_id", "status"])

    if not _has_table("operation_plan_generation_runs"):
        op.create_table(
            "operation_plan_generation_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False, server_default="1"),
            sa.Column("plan_id", sa.Integer(), sa.ForeignKey("operation_plan_templates.id"), nullable=False),
            sa.Column("run_type", sa.String(length=40), nullable=False, server_default="preview"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="success"),
            sa.Column("requested_by", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("request_payload", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("result_payload", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_operation_plan_generation_runs_plan", "operation_plan_generation_runs", ["tenant_id", "plan_id", "created_at"])


def downgrade() -> None:
    for name in (
        "operation_plan_generation_runs",
        "operation_plan_task_links",
        "operation_plan_targets",
        "operation_plan_templates",
    ):
        if _has_table(name):
            op.drop_table(name)
