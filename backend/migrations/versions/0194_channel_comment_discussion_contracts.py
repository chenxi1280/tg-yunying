"""Add authoritative channel discussion contracts.

Revision ID: 0194_channel_comment_discussion
Revises: 0193_comment_business_guards
"""

from alembic import context, op
import sqlalchemy as sa


revision = "0194_channel_comment_discussion"
down_revision = "0193_comment_business_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _create_group_probe_events()
    _create_group_bindings()
    _create_thread_probe_events()
    _create_thread_bindings()
    _create_membership_facts()
    _create_grounding_enrollments()
    _create_listener_error_events()
    _create_recovery_manifests()
    _add_source_revision_columns()
    _add_plan_columns()
    _add_obligation_columns()
    _add_subscription_columns()


def _create_group_probe_events() -> None:
    if _has_table("channel_discussion_group_probe_events"):
        return
    op.create_table(
        "channel_discussion_group_probe_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_reference_revision", sa.Integer(), nullable=False),
        sa.Column("probe_request_id", sa.String(80), nullable=False),
        sa.Column("probe_status", sa.String(32), nullable=False),
        sa.Column("probe_stage", sa.String(48), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id")),
        sa.Column("observed_linked_chat_id", sa.String(160)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fresh_until_at", sa.DateTime(timezone=True)),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "channel_target_id", "probe_request_id", name="uq_channel_discussion_group_probe_request"),
    )


def _create_group_bindings() -> None:
    if _has_table("channel_discussion_group_bindings"):
        return
    op.create_table(
        "channel_discussion_group_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_reference_revision", sa.Integer(), nullable=False),
        sa.Column("binding_revision", sa.Integer(), nullable=False),
        sa.Column("channel_peer_id", sa.String(160), nullable=False),
        sa.Column("discussion_target_id", sa.Integer(), sa.ForeignKey("operation_targets.id")),
        sa.Column("discussion_peer_id", sa.String(160)),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("binding_status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("probe_event_id", sa.String(36), sa.ForeignKey("channel_discussion_group_probe_events.id"), nullable=False),
        sa.Column("supersedes_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_group_bindings.id", ondelete="RESTRICT")),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fresh_until_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "channel_target_id", "binding_revision", name="uq_channel_discussion_group_binding_revision"),
    )
    op.create_index(
        "uq_channel_discussion_group_binding_current", "channel_discussion_group_bindings",
        ["tenant_id", "channel_target_id"], unique=True, postgresql_where=sa.text("is_current = true"), sqlite_where=sa.text("is_current = 1"),
    )


def _create_thread_probe_events() -> None:
    if _has_table("channel_discussion_thread_probe_events"):
        return
    op.create_table(
        "channel_discussion_thread_probe_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_revision_id", sa.String(36), sa.ForeignKey("channel_message_source_revisions.id"), nullable=False),
        sa.Column("group_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_group_bindings.id"), nullable=False),
        sa.Column("probe_request_id", sa.String(80), nullable=False),
        sa.Column("probe_status", sa.String(32), nullable=False),
        sa.Column("probe_stage", sa.String(48), nullable=False),
        sa.Column("observed_thread_root_message_id", sa.Integer()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fresh_until_at", sa.DateTime(timezone=True)),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "source_revision_id", "probe_request_id", name="uq_channel_discussion_thread_probe_request"),
    )


def _create_thread_bindings() -> None:
    if _has_table("channel_discussion_thread_bindings"):
        return
    op.create_table(
        "channel_discussion_thread_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_revision_id", sa.String(36), sa.ForeignKey("channel_message_source_revisions.id"), nullable=False),
        sa.Column("group_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_group_bindings.id"), nullable=False),
        sa.Column("thread_revision", sa.Integer(), nullable=False),
        sa.Column("discussion_peer_id", sa.String(160), nullable=False),
        sa.Column("thread_root_message_id", sa.Integer(), nullable=False),
        sa.Column("identity_hash", sa.String(64), nullable=False),
        sa.Column("probe_event_id", sa.String(36), sa.ForeignKey(
            "channel_discussion_thread_probe_events.id", name="fk_thread_binding_probe_event",
        ), nullable=False),
        sa.Column("supersedes_thread_binding_id", sa.String(36), sa.ForeignKey(
            "channel_discussion_thread_bindings.id", ondelete="RESTRICT",
            name="fk_thread_binding_supersedes",
        )),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_revision_id", "group_binding_id", "thread_revision", name="uq_channel_discussion_thread_revision"),
    )
    op.create_index(
        "uq_channel_discussion_thread_current", "channel_discussion_thread_bindings",
        ["source_revision_id", "group_binding_id"], unique=True, postgresql_where=sa.text("is_current = true"), sqlite_where=sa.text("is_current = 1"),
    )


def _create_membership_facts() -> None:
    if _has_table("discussion_membership_facts"):
        return
    op.create_table(
        "discussion_membership_facts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("discussion_peer_id", sa.String(160), nullable=False),
        sa.Column("group_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_group_bindings.id"), nullable=False),
        sa.Column("fact_revision", sa.Integer(), nullable=False),
        sa.Column("membership_status", sa.String(32), nullable=False),
        sa.Column("can_send", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fresh_until_at", sa.DateTime(timezone=True)),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("supersedes_fact_id", sa.String(36), sa.ForeignKey(
            "discussion_membership_facts.id", ondelete="RESTRICT",
            name="fk_disc_membership_supersedes",
        )),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "account_id", "discussion_peer_id", "group_binding_id", "fact_revision", name="uq_discussion_membership_fact_revision"),
    )
    op.create_index(
        "uq_discussion_membership_fact_current", "discussion_membership_facts",
        ["tenant_id", "account_id", "discussion_peer_id", "group_binding_id"], unique=True,
        postgresql_where=sa.text("is_current = true"), sqlite_where=sa.text("is_current = 1"),
    )


def _create_grounding_enrollments() -> None:
    if _has_table("channel_comment_grounding_enrollments"):
        return
    op.create_table(
        "channel_comment_grounding_enrollments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_config_revision", sa.Integer(), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("enrollment_revision", sa.Integer(), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("contracts_hash", sa.String(64), nullable=False),
        sa.Column("group_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_group_bindings.id"), nullable=False),
        sa.Column("group_binding_revision", sa.Integer(), nullable=False),
        sa.Column("group_binding_identity_hash", sa.String(64), nullable=False),
        sa.Column("activation_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("operator_id", sa.String(120), nullable=False),
        sa.Column("approval_reference", sa.String(160), nullable=False),
        sa.Column("enrollment_state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("supersedes_enrollment_id", sa.String(36), sa.ForeignKey("channel_comment_grounding_enrollments.id", ondelete="RESTRICT")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "task_config_revision", "enrollment_revision", name="uq_channel_comment_grounding_enrollment_revision"),
    )
    op.create_index(
        "uq_channel_comment_grounding_enrollment_active", "channel_comment_grounding_enrollments",
        ["task_id", "task_config_revision"], unique=True,
        postgresql_where=sa.text("enrollment_state = 'active'"), sqlite_where=sa.text("enrollment_state = 'active'"),
    )


def _create_listener_error_events() -> None:
    if _has_table("channel_comment_listener_error_events"):
        return
    op.create_table(
        "channel_comment_listener_error_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subscription_id", sa.String(36), sa.ForeignKey("task_source_subscriptions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_reference_revision", sa.Integer(), nullable=False),
        sa.Column("listener_revision", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=False),
        sa.Column("error_state", sa.String(24), nullable=False, server_default="active"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "subscription_id", "target_reference_revision", "listener_revision", "error_code", name="uq_channel_comment_listener_error_owner"),
    )


def _create_recovery_manifests() -> None:
    if _has_table("channel_comment_recovery_manifests"):
        return
    op.create_table(
        "channel_comment_recovery_manifests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recovery_kind", sa.String(64), nullable=False),
        sa.Column("expected_deployed_sha", sa.String(64), nullable=False),
        sa.Column("expected_task_status", sa.String(32), nullable=False),
        sa.Column("expected_task_config_revision", sa.Integer(), nullable=False),
        sa.Column("expected_task_lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("expected_target_reference_revision", sa.Integer(), nullable=False),
        sa.Column("expected_binding_id", sa.String(36)),
        sa.Column("expected_binding_revision", sa.Integer()),
        sa.Column("action_set_hash", sa.String(64), nullable=False),
        sa.Column("exact_action_ids_json", sa.JSON(), nullable=False),
        sa.Column("recovery_evidence_json", sa.JSON(), nullable=False),
        sa.Column("state_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("preview_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("manifest_state", sa.String(24), nullable=False, server_default="previewed"),
        sa.Column("operator_id", sa.String(120), nullable=False),
        sa.Column("approval_reference", sa.String(160), nullable=False),
        sa.Column("previewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.Column("readback_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def _add_source_revision_columns() -> None:
    table = "channel_message_source_revisions"
    _add_columns(table, (
        sa.Column("discussion_group_binding_id", sa.String(36), sa.ForeignKey(
            "channel_discussion_group_bindings.id", ondelete="RESTRICT",
            name="fk_channel_source_rev_group_binding",
        )),
        sa.Column("discussion_group_binding_revision", sa.Integer()),
        sa.Column("discussion_group_identity_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("discussion_thread_binding_id", sa.String(36), sa.ForeignKey(
            "channel_discussion_thread_bindings.id", ondelete="RESTRICT",
            name="fk_channel_source_rev_thread_binding",
        )),
        sa.Column("discussion_thread_revision", sa.Integer()),
        sa.Column("discussion_thread_identity_hash", sa.String(64), nullable=False, server_default=""),
    ))


def _add_plan_columns() -> None:
    table = "channel_comment_plan_contracts"
    _add_columns(table, (
        sa.Column("grounding_enrollment_id", sa.String(36), sa.ForeignKey("channel_comment_grounding_enrollments.id", ondelete="RESTRICT")),
        sa.Column("discussion_group_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_group_bindings.id", ondelete="RESTRICT")),
        sa.Column("discussion_group_binding_revision", sa.Integer()),
        sa.Column("discussion_group_identity_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("discussion_thread_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_thread_bindings.id", ondelete="RESTRICT")),
        sa.Column("discussion_thread_revision", sa.Integer()),
        sa.Column("discussion_thread_identity_hash", sa.String(64), nullable=False, server_default=""),
    ))


def _add_obligation_columns() -> None:
    table = "comment_fulfillment_obligations"
    _add_columns(table, (
        sa.Column("grounding_enrollment_id", sa.String(36), sa.ForeignKey("channel_comment_grounding_enrollments.id", ondelete="RESTRICT")),
        sa.Column("discussion_group_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_group_bindings.id", ondelete="RESTRICT")),
        sa.Column("discussion_group_binding_revision", sa.Integer()),
        sa.Column("discussion_group_identity_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("discussion_thread_binding_id", sa.String(36), sa.ForeignKey("channel_discussion_thread_bindings.id", ondelete="RESTRICT")),
        sa.Column("discussion_thread_revision", sa.Integer()),
        sa.Column("discussion_thread_identity_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("rpc_mode", sa.String(32), nullable=False, server_default=""),
        sa.Column("channel_peer_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("discussion_peer_id", sa.String(160), nullable=False, server_default=""),
        sa.Column("source_remote_message_id", sa.Integer()),
        sa.Column("thread_root_message_id", sa.Integer()),
        sa.Column("membership_fact_id", sa.String(36), sa.ForeignKey("discussion_membership_facts.id", ondelete="RESTRICT")),
        sa.Column("task_config_revision", sa.Integer()),
    ))


def _add_subscription_columns() -> None:
    _add_columns("task_source_subscriptions", (
        sa.Column("target_reference_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("listener_revision", sa.Integer(), nullable=False, server_default="0"),
    ))


def _add_columns(table_name: str, columns: tuple[sa.Column, ...]) -> None:
    for column in columns:
        if not _has_column(table_name, str(column.name)):
            op.add_column(table_name, column)


def _has_table(table_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return sa.inspect(op.get_bind()).has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return any(row["name"] == column_name for row in sa.inspect(op.get_bind()).get_columns(table_name))


def downgrade() -> None:
    _drop_columns("task_source_subscriptions", ("listener_revision", "target_reference_revision"))
    _drop_columns("comment_fulfillment_obligations", _obligation_column_names())
    _drop_columns("channel_comment_plan_contracts", _plan_column_names())
    _drop_columns("channel_message_source_revisions", _source_column_names())
    for table_name in (
        "channel_comment_recovery_manifests", "channel_comment_listener_error_events",
        "channel_comment_grounding_enrollments",
        "discussion_membership_facts", "channel_discussion_thread_bindings",
        "channel_discussion_thread_probe_events", "channel_discussion_group_bindings",
        "channel_discussion_group_probe_events",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)


def _drop_columns(table_name: str, column_names: tuple[str, ...]) -> None:
    for column_name in column_names:
        if _has_column(table_name, column_name):
            op.drop_column(table_name, column_name)


def _source_column_names() -> tuple[str, ...]:
    return (
        "discussion_thread_identity_hash", "discussion_thread_revision", "discussion_thread_binding_id",
        "discussion_group_identity_hash", "discussion_group_binding_revision", "discussion_group_binding_id",
    )


def _plan_column_names() -> tuple[str, ...]:
    return _source_column_names() + ("grounding_enrollment_id",)


def _obligation_column_names() -> tuple[str, ...]:
    return (
        "task_config_revision", "membership_fact_id", "thread_root_message_id",
        "source_remote_message_id", "discussion_peer_id", "channel_peer_id", "rpc_mode",
    ) + _plan_column_names()
