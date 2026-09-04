"""Add unified engagement runtime resource owners.

Revision ID: 0198_unified_engagement_runtime
Revises: 0197_unified_engagement_binding
"""

from alembic import op
import sqlalchemy as sa


revision = "0198_unified_engagement_runtime"
down_revision = "0197_unified_engagement_binding"
branch_labels = None
depends_on = None


def _policy_columns() -> list[sa.Column]:
    return [
        sa.Column("state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("effective_to", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    _create_resilience_policy()
    _create_pool_policy_and_lease()
    _create_behavior_budget()
    _create_remote_fence()


def _create_resilience_policy() -> None:
    table = "execution_resilience_policy_revisions"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("telegram_connect_timeout_seconds", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("telegram_gateway_timeout_seconds", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("llm_invocation_timeout_seconds", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("proxy_route_inflight_limit", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("proxy_egress_inflight_limit", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("circuit_window_seconds", sa.Integer(), nullable=False, server_default="300"),
        sa.Column("circuit_failure_threshold", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("circuit_open_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("task_contention_base_cap_bps", sa.Integer(), nullable=False, server_default="3000"),
        *_policy_columns(),
        sa.UniqueConstraint("tenant_id", "revision", name="uq_execution_resilience_policy_revision"),
    )
    _active_index("uq_execution_resilience_policy_active", table, ["tenant_id"])


def _create_pool_policy_and_lease() -> None:
    policy = "account_pool_concurrency_policy_revisions"
    op.create_table(
        policy,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("hard_remote_inflight_limit", sa.Integer(), nullable=False),
        sa.Column("workload_policy", sa.JSON(), nullable=False, server_default="{}"),
        *_policy_columns(),
        sa.UniqueConstraint("tenant_id", "account_pool_id", "revision", name="uq_pool_concurrency_policy_revision"),
    )
    _active_index("uq_pool_concurrency_policy_active", policy, ["tenant_id", "account_pool_id"])
    lease = "account_pool_concurrency_leases"
    op.create_table(
        lease,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("policy_revision_id", sa.String(36), sa.ForeignKey(f"{policy}.id"), nullable=False),
        sa.Column("account_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("execution_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invocation_identity", sa.String(160), nullable=False),
        sa.Column("task_group_share_limit", sa.Integer(), nullable=False),
        sa.Column("fencing_token", sa.String(80), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(80), nullable=False, server_default=""),
        sa.UniqueConstraint("account_pool_id", "invocation_identity", name="uq_pool_concurrency_invocation"),
    )
    op.create_index("ix_pool_concurrency_active", lease, ["account_pool_id", "state", "acquired_at"])
    op.create_index("ix_pool_concurrency_task_share", lease, ["task_id", "account_pool_id", "state"])


def _create_behavior_budget() -> None:
    _create_behavior_budget_policy()
    _create_behavior_budget_storage()


def _create_behavior_budget_policy() -> None:
    policy = "account_behavior_budget_policy_revisions"
    op.create_table(
        policy,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_class", sa.String(40), nullable=False, server_default="normal"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("action_budgets", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("session_budget", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("wake_budget", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pair_gap_policy", sa.JSON(), nullable=False, server_default="{}"),
        *_policy_columns(),
        sa.UniqueConstraint("tenant_id", "account_class", "revision", name="uq_behavior_budget_policy_revision"),
    )
    _active_index("uq_behavior_budget_policy_active", policy, ["tenant_id", "account_class"])


def _create_behavior_budget_storage() -> None:
    policy = "account_behavior_budget_policy_revisions"
    ledger = "account_behavior_budget_ledgers"
    op.create_table(
        ledger,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_day", sa.Date(), nullable=False),
        sa.Column("policy_revision_id", sa.String(36), sa.ForeignKey(f"{policy}.id"), nullable=False),
        sa.Column("action_budgets", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("counters", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("session_counters", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("wake_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rest_debt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "account_id", "task_day", name="uq_behavior_budget_account_day"),
    )
    op.create_index("ix_behavior_budget_task_day", ledger, ["tenant_id", "task_day", "account_id"])
    reservation = "account_behavior_budget_reservations"
    op.create_table(
        reservation,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ledger_id", sa.String(36), sa.ForeignKey(f"{ledger}.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("execution_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_class", sa.String(40), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("attempt_id", name="uq_behavior_budget_attempt"),
    )
    op.create_index("ix_behavior_budget_reservation_state", reservation, ["ledger_id", "action_class", "state"])


def _create_remote_fence() -> None:
    table = "remote_invocation_fences"
    op.create_table(
        table,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_id", sa.String(36), sa.ForeignKey("actions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(36), sa.ForeignKey("execution_attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("invocation_identity", sa.String(160), nullable=False),
        sa.Column("invocation_kind", sa.String(40), nullable=False),
        sa.Column("domain_keys", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("resilience_policy_revision_id", sa.String(36), sa.ForeignKey("execution_resilience_policy_revisions.id"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="reserved"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transport_terminated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("business_outcome_state", sa.String(32), nullable=False, server_default="not_called"),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("invocation_identity", name="uq_remote_invocation_fence_identity"),
    )
    op.create_index("ix_remote_invocation_fence_active", table, ["state", "invocation_kind", "started_at"])


def _active_index(name: str, table: str, columns: list[str]) -> None:
    op.create_index(
        name,
        table,
        columns,
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
        sqlite_where=sa.text("state = 'active'"),
    )


def downgrade() -> None:
    for table in [
        "remote_invocation_fences",
        "account_behavior_budget_reservations",
        "account_behavior_budget_ledgers",
        "account_behavior_budget_policy_revisions",
        "account_pool_concurrency_leases",
        "account_pool_concurrency_policy_revisions",
        "execution_resilience_policy_revisions",
    ]:
        op.drop_table(table)
