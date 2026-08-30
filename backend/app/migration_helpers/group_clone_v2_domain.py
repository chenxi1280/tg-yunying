from alembic import op
import sqlalchemy as sa


def upgrade_clone_tables(existing_tables: set[str]) -> None:
    _upgrade_clone_source_stream_states(existing_tables)
    _upgrade_clone_source_events(existing_tables)
    _upgrade_clone_target_route_snapshots(existing_tables)
    _upgrade_clone_target_execution_snapshots(existing_tables)
    _upgrade_clone_account_slots(existing_tables)
    _upgrade_clone_sender_binding_history(existing_tables)
    _upgrade_clone_album_manifests(existing_tables)
    _upgrade_clone_album_items(existing_tables)
    _upgrade_clone_topic_maps(existing_tables)
    _upgrade_telegram_gateway_mutation_identities(existing_tables)
    _upgrade_clone_delivery_obligations(existing_tables)
    _upgrade_clone_message_parts(existing_tables)
    _upgrade_clone_manual_review_decisions(existing_tables)
    _upgrade_clone_sequencer_head_cases(existing_tables)
    _upgrade_clone_cutover_exclusions(existing_tables)


def _upgrade_clone_source_stream_states(existing_tables: set[str]) -> None:
    if "clone_source_stream_states" in existing_tables:
        return
    op.create_table(
        "clone_source_stream_states",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_peer_type", sa.String(length=32), nullable=False),
        sa.Column("source_peer_id", sa.String(length=120), nullable=False),
        sa.Column("listener_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_message_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("start_pts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("authorization_update_state_id", sa.String(length=36), nullable=True),
        sa.Column("last_consumed_ingress_order_no", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("channel_pts", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("difference_cursor", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="initializing"),
        sa.Column("owner_id", sa.String(length=64), nullable=True),
        sa.Column("owner_fencing_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_applied_event_hash", sa.String(length=64), nullable=True),
        sa.Column("last_applied_stream_order_no", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "task_id", "task_lifecycle_epoch", name="uq_clone_stream_task_epoch"),
    )
    op.create_index("ix_clone_stream_state_lease", "clone_source_stream_states", ["owner_id", "lease_expires_at"])


def _upgrade_clone_source_events(existing_tables: set[str]) -> None:
    # 10. clone_source_events
    if "clone_source_events" not in existing_tables:
        op.create_table(
            "clone_source_events",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_peer_type", sa.String(length=32), nullable=False),
            sa.Column("source_peer_id", sa.String(length=120), nullable=False),
            sa.Column("source_message_id", sa.BigInteger(), nullable=False),
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column("ingress_update_identity_hash", sa.String(length=64), nullable=True),
            sa.Column("event_identity_hash", sa.String(length=64), nullable=False),
            sa.Column("source_pts", sa.BigInteger(), nullable=True),
            sa.Column("source_pts_count", sa.Integer(), nullable=True),
            sa.Column("authorization_ingress_order_no", sa.BigInteger(), nullable=True),
            sa.Column("normalized_item_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("apply_order_key", sa.String(length=160), nullable=False),
            sa.Column("stream_order_no", sa.BigInteger(), nullable=False),
            sa.Column("message_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("sender_peer_type", sa.String(length=32), nullable=True),
            sa.Column("sender_peer_id", sa.String(length=120), nullable=True),
            sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
            sa.Column("source_top_message_id", sa.BigInteger(), nullable=True),
            sa.Column("grouped_id", sa.String(length=64), nullable=True),
            sa.Column("media_type", sa.String(length=32), nullable=True),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("entities", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("poll_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("content_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("protected_content", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("sanitization_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "task_id", "task_lifecycle_epoch", "source_peer_type", "source_peer_id", "event_identity_hash", name="uq_clone_source_event_identity"),
            sa.UniqueConstraint("tenant_id", "task_id", "task_lifecycle_epoch", "stream_order_no", name="uq_clone_source_stream_order"),
        )
        op.create_index("ix_clone_source_event_grouped", "clone_source_events", ["task_id", "grouped_id"])
        op.create_index("ix_clone_source_event_msg", "clone_source_events", ["task_id", "source_message_id"])


def _upgrade_clone_target_route_snapshots(existing_tables: set[str]) -> None:
    # 11. clone_target_route_snapshots
    if "clone_target_route_snapshots" not in existing_tables:
        op.create_table(
            "clone_target_route_snapshots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("route_binding_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_internal_group_id", sa.Integer(), nullable=True),
            sa.Column("source_operation_target_id", sa.String(length=36), nullable=True),
            sa.Column("source_peer_type", sa.String(length=32), nullable=False),
            sa.Column("source_peer_id", sa.String(length=120), nullable=False),
            sa.Column("target_internal_group_id", sa.Integer(), nullable=True),
            sa.Column("target_operation_target_id", sa.String(length=36), nullable=True),
            sa.Column("target_peer_type", sa.String(length=32), nullable=False),
            sa.Column("target_peer_id", sa.String(length=120), nullable=False),
            sa.Column("reply_target_message_id", sa.BigInteger(), nullable=True),
            sa.Column("target_top_msg_id", sa.BigInteger(), nullable=True),
            sa.Column("route_binding_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("task_id", "epoch", "route_binding_version", name="uq_clone_route_snapshot_version"),
        )


def _upgrade_clone_target_execution_snapshots(existing_tables: set[str]) -> None:
    # 12. clone_target_execution_snapshots
    if "clone_target_execution_snapshots" not in existing_tables:
        op.create_table(
            "clone_target_execution_snapshots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("route_snapshot_id", sa.String(length=36), sa.ForeignKey("clone_target_route_snapshots.id", ondelete="CASCADE"), nullable=False),
            sa.Column("execution_binding_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("execution_role", sa.String(length=32), nullable=False, server_default="sender"),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("session_generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("account_target_relation_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("sender_binding_history_id", sa.String(length=36), nullable=True),
            sa.Column("sender_binding_version", sa.Integer(), nullable=True),
            sa.Column("execution_binding_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("route_snapshot_id", "execution_binding_version", name="uq_clone_execution_snapshot_ver"),
        )


def _upgrade_clone_account_slots(existing_tables: set[str]) -> None:
    # 13. clone_account_slots
    if "clone_account_slots" not in existing_tables:
        op.create_table(
            "clone_account_slots",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="available"),
            sa.Column("projected_transport_blocked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("owner_id", sa.String(length=64), nullable=True),
            sa.Column("owner_fencing_epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("task_id", "account_id", name="uq_clone_account_slot_task_account"),
        )
        op.create_index("ix_clone_account_slot_state", "clone_account_slots", ["task_id", "state"])


def _upgrade_clone_sender_binding_history(existing_tables: set[str]) -> None:
    # 14. clone_sender_binding_history
    if "clone_sender_binding_history" not in existing_tables:
        op.create_table(
            "clone_sender_binding_history",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("binding_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_sender_peer_type", sa.String(length=32), nullable=False),
            sa.Column("source_sender_peer_id", sa.String(length=120), nullable=False),
            sa.Column("source_sender_name", sa.String(length=160), nullable=False, server_default=""),
            sa.Column("assigned_account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("account_slot_id", sa.String(length=36), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("is_vip", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_spoken_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("last_reassigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("reassignment_reason", sa.String(length=100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_clone_sender_binding_task_status", "clone_sender_binding_history", ["task_id", "status"])
        op.create_index(
            "uq_clone_active_sender_slot",
            "clone_sender_binding_history",
            ["task_id", "task_lifecycle_epoch", "source_sender_peer_type", "source_sender_peer_id"],
            unique=True,
            postgresql_where=sa.text("status IN ('active', 'guarded', 'eligible')"),
            sqlite_where=sa.text("status IN ('active', 'guarded', 'eligible')"),
        )
        op.create_index(
            "uq_clone_active_account_slot",
            "clone_sender_binding_history",
            ["task_id", "task_lifecycle_epoch", "assigned_account_id"],
            unique=True,
            postgresql_where=sa.text("status IN ('active', 'guarded', 'eligible')"),
            sqlite_where=sa.text("status IN ('active', 'guarded', 'eligible')"),
        )


def _upgrade_clone_album_manifests(existing_tables: set[str]) -> None:
    # 15. clone_album_manifests
    if "clone_album_manifests" not in existing_tables:
        op.create_table(
            "clone_album_manifests",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("grouped_id", sa.String(length=64), nullable=False),
            sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("quiet_deadline_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("max_deadline_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("items_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("collection_fingerprint", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="collecting"),
            sa.Column("frozen_policy", sa.String(length=32), nullable=False, server_default="drop_incomplete"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("task_id", "epoch", "grouped_id", name="uq_clone_album_manifest_grouped"),
        )
        op.create_index("ix_clone_album_manifest_state", "clone_album_manifests", ["task_id", "state"])


def _upgrade_clone_album_items(existing_tables: set[str]) -> None:
    # 16. clone_album_items
    if "clone_album_items" not in existing_tables:
        op.create_table(
            "clone_album_items",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("manifest_id", sa.String(length=36), sa.ForeignKey("clone_album_manifests.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_event_id", sa.String(length=36), sa.ForeignKey("clone_source_events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("part_index", sa.Integer(), nullable=False),
            sa.Column("source_message_id", sa.BigInteger(), nullable=False),
            sa.Column("media_type", sa.String(length=32), nullable=False),
            sa.Column("media_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("item_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("acquisition_state", sa.String(length=32), nullable=False, server_default="acquired"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("manifest_id", "part_index", name="uq_clone_album_item_part"),
        )


def _upgrade_clone_topic_maps(existing_tables: set[str]) -> None:
    # 17. clone_topic_maps
    if "clone_topic_maps" not in existing_tables:
        op.create_table(
            "clone_topic_maps",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_peer_type", sa.String(length=32), nullable=False),
            sa.Column("source_peer_id", sa.String(length=120), nullable=False),
            sa.Column("source_top_message_id", sa.BigInteger(), nullable=False),
            sa.Column("target_top_message_id", sa.BigInteger(), nullable=True),
            sa.Column("topic_title_fingerprint", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("topic_icon_fingerprint", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="placeholder"),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("task_id", "epoch", "source_peer_type", "source_peer_id", "source_top_message_id", name="uq_clone_topic_map_source"),
        )


def _upgrade_telegram_gateway_mutation_identities(existing_tables: set[str]) -> None:
    # 18. telegram_gateway_mutation_identities
    if "telegram_gateway_mutation_identities" not in existing_tables:
        op.create_table(
            "telegram_gateway_mutation_identities",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("obligation_id", sa.String(length=255), nullable=False),
            sa.Column("materialization_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("mutation_kind", sa.String(length=40), nullable=False),
            sa.Column("part_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("execution_role", sa.String(length=32), nullable=False, server_default="sender"),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("telegram_account_peer_id", sa.String(length=120), nullable=False),
            sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("session_generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("target_peer_type", sa.String(length=32), nullable=False),
            sa.Column("target_peer_id", sa.String(length=120), nullable=False),
            sa.Column("random_id", sa.BigInteger(), nullable=True),
            sa.Column("derivation_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("collision_nonce", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="allocated"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("task_id", "epoch", "obligation_id", "materialization_version", "mutation_kind", "part_index", name="uq_gateway_mutation_identity_scope"),
        )
        op.create_index(
            "uq_gateway_mutation_random_id_peer",
            "telegram_gateway_mutation_identities",
            ["tenant_id", "telegram_account_peer_id", "target_peer_type", "target_peer_id", "random_id"],
            unique=True,
            postgresql_where=sa.text("random_id IS NOT NULL"),
            sqlite_where=sa.text("random_id IS NOT NULL"),
        )


def _upgrade_clone_delivery_obligations(existing_tables: set[str]) -> None:
    # 19. clone_delivery_obligations
    if "clone_delivery_obligations" not in existing_tables:
        op.create_table(
            "clone_delivery_obligations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_event_id", sa.String(length=36), sa.ForeignKey("clone_source_events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("source_message_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("obligation_kind", sa.String(length=32), nullable=False),
            sa.Column("materialization_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("stream_order_no", sa.BigInteger(), nullable=False),
            sa.Column("sequencer_id", sa.BigInteger(), nullable=False),
            sa.Column("dependency_obligation_id", sa.String(length=36), nullable=True),
            sa.Column("dependency_source_msg_id", sa.BigInteger(), nullable=True),
            sa.Column("binding_history_id", sa.String(length=36), nullable=True),
            sa.Column("route_binding_snapshot_id", sa.String(length=36), nullable=True),
            sa.Column("execution_target_binding_snapshot_id", sa.String(length=36), nullable=True),
            sa.Column("album_manifest_id", sa.String(length=36), nullable=True),
            sa.Column("topic_map_id", sa.String(length=36), nullable=True),
            sa.Column("sequencer_head_case_id", sa.String(length=36), nullable=True),
            sa.Column("config_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("sanitization_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("media_policy_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("contract_version", sa.String(length=32), nullable=False, server_default="v2_group_clone"),
            sa.Column("planned_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("unknown_deadline_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="observed"),
            sa.Column("degradation_reason", sa.String(length=80), nullable=True),
            sa.Column("error_code", sa.String(length=80), nullable=True),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.UniqueConstraint("task_id", "epoch", "source_event_id", "obligation_kind", "materialization_version", name="uq_clone_obligation_materialization"),
            sa.UniqueConstraint("task_id", "epoch", "sequencer_id", name="uq_clone_obligation_sequencer"),
        )
        op.create_index("ix_clone_obligation_state", "clone_delivery_obligations", ["task_id", "state"])
        op.create_index("ix_clone_obligation_stream_order", "clone_delivery_obligations", ["task_id", "stream_order_no"])


def _upgrade_clone_message_parts(existing_tables: set[str]) -> None:
    # 20. clone_message_parts
    if "clone_message_parts" not in existing_tables:
        op.create_table(
            "clone_message_parts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("obligation_id", sa.String(length=36), sa.ForeignKey("clone_delivery_obligations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("action_id", sa.String(length=36), nullable=False),
            sa.Column("attempt_id", sa.String(length=36), nullable=False),
            sa.Column("remote_fact_id", sa.String(length=36), nullable=False),
            sa.Column("part_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("part_total", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_message_id", sa.BigInteger(), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("session_generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("execution_binding_hash", sa.String(length=64), nullable=False),
            sa.Column("target_peer_type", sa.String(length=32), nullable=False),
            sa.Column("target_peer_id", sa.String(length=120), nullable=False),
            sa.Column("target_message_id", sa.BigInteger(), nullable=False),
            sa.Column("target_top_message_id", sa.BigInteger(), nullable=True),
            sa.Column("gateway_mutation_identity_id", sa.String(length=36), nullable=False),
            sa.Column("random_id", sa.BigInteger(), nullable=True),
            sa.Column("gateway_request_identity", sa.String(length=64), nullable=False),
            sa.Column("remote_confirmed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("task_id", "epoch", "obligation_id", "part_index", name="uq_clone_msg_part_index"),
        )
        op.create_index("ix_clone_msg_part_remote", "clone_message_parts", ["target_peer_type", "target_peer_id", "target_message_id"])
        op.create_index("ix_clone_msg_part_source_msg", "clone_message_parts", ["task_id", "source_message_id"])


def _upgrade_clone_manual_review_decisions(existing_tables: set[str]) -> None:
    # 21. clone_manual_review_decisions
    if "clone_manual_review_decisions" not in existing_tables:
        op.create_table(
            "clone_manual_review_decisions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("obligation_id", sa.String(length=36), sa.ForeignKey("clone_delivery_obligations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("review_revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("client_request_id", sa.String(length=100), nullable=False),
            sa.Column("decision", sa.String(length=32), nullable=False),
            sa.Column("actor_id", sa.Integer(), nullable=True),
            sa.Column("actor_name", sa.String(length=100), nullable=False, server_default=""),
            sa.Column("reason", sa.String(length=255), nullable=False, server_default=""),
            sa.Column("before_fingerprint", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("after_fingerprint", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("obligation_id", "review_revision", name="uq_clone_manual_review_rev"),
            sa.UniqueConstraint("obligation_id", "client_request_id", name="uq_clone_manual_review_request"),
        )


def _upgrade_clone_sequencer_head_cases(existing_tables: set[str]) -> None:
    # 22. clone_sequencer_head_cases
    if "clone_sequencer_head_cases" not in existing_tables:
        op.create_table(
            "clone_sequencer_head_cases",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("sequencer_id", sa.BigInteger(), nullable=False),
            sa.Column("obligation_id", sa.String(length=36), sa.ForeignKey("clone_delivery_obligations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("case_kind", sa.String(length=32), nullable=False),
            sa.Column("failure_evidence", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("remote_mutation_started", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("authoritative_absence_evidence_id", sa.String(length=36), nullable=True),
            sa.Column("policy_snapshot", sa.String(length=32), nullable=False, server_default="fail_stop"),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="waiting_decision"),
            sa.Column("decision_reason", sa.String(length=255), nullable=True),
            sa.Column("decision_actor_id", sa.Integer(), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("task_id", "epoch", "sequencer_id", "case_kind", name="uq_clone_seq_head_case_scope"),
        )


def _upgrade_clone_cutover_exclusions(existing_tables: set[str]) -> None:
    # 23. clone_cutover_exclusions
    if "clone_cutover_exclusions" not in existing_tables:
        op.create_table(
            "clone_cutover_exclusions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("cutover_generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_event_identity_hash", sa.String(length=64), nullable=False),
            sa.Column("legacy_task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("clone_task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("mutation_owner_side", sa.String(length=32), nullable=False),
            sa.Column("legacy_action_id", sa.String(length=36), nullable=True),
            sa.Column("clone_obligation_id", sa.String(length=36), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("cutover_generation", "source_event_identity_hash", name="uq_clone_cutover_exclusion_identity"),
        )





__all__ = ["upgrade_clone_tables"]
