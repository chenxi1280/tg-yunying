"""Add AI content window, generation snapshots, attempts, and capacity plans.

Revision ID: 0156_ai_content_runtime
Revises: 0155_ai_content_policy_routes
"""

from __future__ import annotations

from collections.abc import Callable

from alembic import op
import sqlalchemy as sa


revision = "0156_ai_content_runtime"
down_revision = "0155_ai_content_policy_routes"
branch_labels = None
depends_on = None
ColumnFactory = Callable[[], sa.Column]
OWNER_TABLES = (
    "task_group_daily_message_slots",
    "comment_fulfillment_obligations",
    "reaction_fulfillment_obligations",
    "view_fulfillment_obligations",
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _columns(name: str) -> set[str]:
    if not _has_table(name):
        return set()
    return {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(name)}


def _add_columns(table: str, factories: tuple[ColumnFactory, ...]) -> None:
    existing = _columns(table)
    for factory in factories:
        column = factory()
        if existing and str(column.name) not in existing:
            op.add_column(table, column)


def _generation_columns() -> tuple[ColumnFactory, ...]:
    strings = (
        ("task_binding_hash", 64),
        ("window_plan_hash", 64),
        ("task_direction_snapshot_hash", 64),
        ("content_policy_hash", 64),
        ("context_route", 40),
        ("content_mode", 64),
        ("route_evidence_hash", 64),
        ("prompt_contract_version", 80),
        ("example_set_version", 80),
        ("voice_profile_version", 80),
        ("provider_route_set_hash", 64),
        ("request_hash", 64),
        ("generation_stage", 32),
    )
    result: list[ColumnFactory] = [
        lambda: sa.Column("window_slot_id", sa.String(36), nullable=True),
        lambda: sa.Column("provider_route_set_id", sa.String(36), nullable=True),
        lambda: sa.Column("provider_route_set_revision", sa.Integer(), nullable=False, server_default="0"),
        lambda: sa.Column("provider_route_snapshots", sa.JSON(), nullable=False, server_default="{}"),
        lambda: sa.Column("stage_version", sa.Integer(), nullable=False, server_default="1"),
        lambda: sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        lambda: sa.Column("latest_safe_send_at", sa.DateTime(timezone=True), nullable=True),
    ]
    result.extend(
        lambda name=name, size=size: sa.Column(
            name,
            sa.String(size),
            nullable=False,
            server_default="routing" if name == "generation_stage" else "",
        )
        for name, size in strings
    )
    return tuple(result)


def _owner_columns() -> tuple[ColumnFactory, ...]:
    return (
        lambda: sa.Column("source_capacity_plan_hash", sa.String(64), nullable=True),
        lambda: sa.Column("source_capacity_slot_ordinal", sa.Integer(), nullable=True),
    )


def _create_window_tables() -> None:
    if not _has_table("ai_content_window_plans"):
        op.create_table(
            "ai_content_window_plans",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("scope_type", sa.String(24), nullable=False),
            sa.Column("scope_id", sa.String(160), nullable=False),
            sa.Column("pacing_plan_hash", sa.String(64), nullable=False),
            sa.Column("period_key", sa.String(80), nullable=False),
            sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("task_config_revision", sa.Integer(), nullable=False),
            sa.Column("content_policy_hash", sa.String(64), nullable=False),
            sa.Column("state", sa.String(24), nullable=False, server_default="draft"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("plan_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("scope_type IN ('group','comment_source')", name="ck_ai_content_window_scope_type"),
            sa.UniqueConstraint(
                "tenant_id", "task_id", "task_lifecycle_epoch", "scope_type", "scope_id",
                "pacing_plan_hash", "period_key", "window_start_at", "window_end_at",
                "task_config_revision", "content_policy_hash", name="uq_ai_content_window_scope",
            ),
        )
    if _has_table("ai_content_window_plan_slots"):
        return
    op.create_table(
        "ai_content_window_plan_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("ai_content_window_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slot_ordinal", sa.Integer(), nullable=False),
        sa.Column("slot_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("obligation_type", sa.String(48), nullable=False),
        sa.Column("obligation_id", sa.String(255), nullable=False),
        sa.Column("generation_sequence", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("context_scope_revision", sa.Integer(), nullable=False),
        sa.Column("context_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("context_route", sa.String(40), nullable=False),
        sa.Column("content_mode", sa.String(64), nullable=False),
        sa.Column("route_evidence_hash", sa.String(64), nullable=False),
        sa.Column("prompt_contract_version", sa.String(80), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="frozen"),
        sa.Column("claimed_by_job_id", sa.String(36), sa.ForeignKey("generation_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("plan_id", "slot_ordinal", "slot_revision", name="uq_ai_content_window_slot_revision"),
    )
    op.create_index("ix_ai_content_window_slot_claim", "ai_content_window_plan_slots", ["state", "due_at", "lease_expires_at"])
    op.create_index(
        "uq_ai_content_window_current_obligation",
        "ai_content_window_plan_slots",
        ["obligation_type", "obligation_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('frozen','claimed','candidate_ready','gateway_bound')"),
        sqlite_where=sa.text("state IN ('frozen','claimed','candidate_ready','gateway_bound')"),
    )


def _create_attempts_and_shortfalls() -> None:
    if not _has_table("ai_provider_attempts"):
        op.create_table(
            "ai_provider_attempts",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("generation_job_id", sa.String(36), sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("purpose", sa.String(64), nullable=False),
            sa.Column("route_set_id", sa.String(36), sa.ForeignKey("tenant_ai_provider_route_sets.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("route_set_revision", sa.Integer(), nullable=False),
            sa.Column("provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("model_name", sa.String(120), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False),
            sa.Column("attempt_index", sa.Integer(), nullable=False),
            sa.Column("request_hash", sa.String(64), nullable=False),
            sa.Column("outcome", sa.String(40), nullable=False),
            sa.Column("error_code", sa.String(80), nullable=False, server_default=""),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("completion_tokens", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("cost_amount", sa.Float(), nullable=False, server_default="0"),
            sa.Column("currency", sa.String(16), nullable=False, server_default="CNY"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("generation_job_id", "purpose", "attempt_index", name="uq_ai_provider_attempt_job_index"),
        )
        op.create_index("ix_ai_provider_attempt_route", "ai_provider_attempts", ["route_set_id", "provider_id", "created_at"])
    if _has_table("fulfillment_shortfall_facts"):
        return
    op.create_table(
        "fulfillment_shortfall_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("owner_type", sa.String(48), nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("period_key", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(80), nullable=False),
        sa.Column("requested_quantity", sa.Integer(), nullable=False),
        sa.Column("settled_quantity", sa.Integer(), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_type", "owner_id", "period_key", name="uq_fulfillment_shortfall_owner_period"),
    )
    op.create_index("ix_fulfillment_shortfall_task", "fulfillment_shortfall_facts", ["tenant_id", "task_id", "settled_at"])


def _create_capacity_tables() -> None:
    if not _has_table("source_pacing_capacity_policy_versions"):
        op.create_table(
            "source_pacing_capacity_policy_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("pacing_domain", sa.String(40), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("hourly_curve", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("minimum_gap_seconds", sa.Integer(), nullable=False),
            sa.Column("hourly_ceiling", sa.Integer(), nullable=False),
            sa.Column("telemetry_window", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("headroom_floor", sa.Float(), nullable=False),
            sa.Column("provider_retry_slots", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("approved_by", sa.String(160), nullable=False, server_default=""),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "pacing_domain", "revision", name="uq_source_capacity_policy_revision"),
        )
        op.create_index(
            "uq_source_capacity_policy_active", "source_pacing_capacity_policy_versions",
            ["tenant_id", "pacing_domain"], unique=True,
            postgresql_where=sa.text("status = 'active'"), sqlite_where=sa.text("status = 'active'"),
        )
    if _has_table("source_pacing_capacity_plans"):
        return
    op.create_table(
        "source_pacing_capacity_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pacing_domain", sa.String(40), nullable=False),
        sa.Column("source_key_hash", sa.String(64), nullable=False),
        sa.Column("window_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version_id", sa.String(36), sa.ForeignKey("source_pacing_capacity_policy_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("curve_hash", sa.String(64), nullable=False),
        sa.Column("capacity_slots", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("occupied_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("incoming_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replacement_headroom", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deficit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_safe_release_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "tenant_id", "pacing_domain", "source_key_hash", "window_start_at", "window_end_at",
            "policy_version_id", "revision", name="uq_source_capacity_plan_scope",
        ),
    )
    op.create_index("ix_source_capacity_plan_window", "source_pacing_capacity_plans", ["window_start_at", "window_end_at", "state"])


def upgrade() -> None:
    _add_columns("generation_jobs", _generation_columns())
    for table in OWNER_TABLES:
        _add_columns(table, _owner_columns())
    _create_window_tables()
    _create_attempts_and_shortfalls()
    _create_capacity_tables()


def downgrade() -> None:
    for table in (
        "source_pacing_capacity_plans",
        "source_pacing_capacity_policy_versions",
        "fulfillment_shortfall_facts",
        "ai_provider_attempts",
        "ai_content_window_plan_slots",
        "ai_content_window_plans",
    ):
        if _has_table(table):
            op.drop_table(table)
    for table in reversed(OWNER_TABLES):
        for factory in reversed(_owner_columns()):
            column = factory()
            if str(column.name) in _columns(table):
                op.drop_column(table, str(column.name))
    for factory in reversed(_generation_columns()):
        column = factory()
        if str(column.name) in _columns("generation_jobs"):
            op.drop_column("generation_jobs", str(column.name))
