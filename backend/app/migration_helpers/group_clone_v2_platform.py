from alembic import op
import sqlalchemy as sa


def upgrade_platform_tables(existing_tables: set[str]) -> None:
    _upgrade_telegram_authorization_update_states(existing_tables)
    _upgrade_telegram_authorization_update_events(existing_tables)
    _upgrade_telegram_authorization_update_subscriptions(existing_tables)
    _upgrade_telegram_authorization_update_deliveries(existing_tables)
    _upgrade_telegram_outbound_random_id_mappings(existing_tables)
    _upgrade_telegram_group_mutation_authorities(existing_tables)
    _upgrade_telegram_group_mutation_authority_holders(existing_tables)
    _upgrade_telegram_authorization_transport_states(existing_tables)


def _upgrade_telegram_authorization_update_states(existing_tables: set[str]) -> None:
    if "telegram_authorization_update_states" in existing_tables:
        return
    op.create_table(
        "telegram_authorization_update_states",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("common_pts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("common_qts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("common_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("common_date", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("difference_cursor", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("state", sa.String(length=32), nullable=False, server_default="initializing"),
        sa.Column("owner_id", sa.String(length=64), nullable=True),
        sa.Column("owner_fencing_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ingress_order_no", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_update_identity_hash", sa.String(length=64), nullable=True),
        sa.Column("last_applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "authorization_id", "session_generation", name="uq_auth_update_state_scope"),
    )
    op.create_index("ix_auth_update_state_lease", "telegram_authorization_update_states", ["owner_id", "lease_expires_at"])


def _upgrade_telegram_authorization_update_events(existing_tables: set[str]) -> None:
    # 2. telegram_authorization_update_events
    if "telegram_authorization_update_events" not in existing_tables:
        op.create_table(
            "telegram_authorization_update_events",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("authorization_update_state_id", sa.String(length=36), sa.ForeignKey("telegram_authorization_update_states.id", ondelete="CASCADE"), nullable=False),
            sa.Column("ingress_order_no", sa.BigInteger(), nullable=False),
            sa.Column("update_identity_hash", sa.String(length=64), nullable=False),
            sa.Column("constructor_name", sa.String(length=80), nullable=False),
            sa.Column("pts_evidence", sa.Integer(), nullable=True),
            sa.Column("pts_count_evidence", sa.Integer(), nullable=True),
            sa.Column("routing_peer_type", sa.String(length=32), nullable=True),
            sa.Column("routing_peer_id", sa.String(length=120), nullable=True),
            sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("authorization_update_state_id", "ingress_order_no", name="uq_auth_update_event_order"),
            sa.UniqueConstraint("authorization_update_state_id", "update_identity_hash", name="uq_auth_update_event_identity"),
        )
        op.create_index("ix_auth_update_event_peer", "telegram_authorization_update_events", ["routing_peer_type", "routing_peer_id"])


def _upgrade_telegram_authorization_update_subscriptions(existing_tables: set[str]) -> None:
    # 3. telegram_authorization_update_subscriptions
    if "telegram_authorization_update_subscriptions" not in existing_tables:
        op.create_table(
            "telegram_authorization_update_subscriptions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("authorization_update_state_id", sa.String(length=36), sa.ForeignKey("telegram_authorization_update_states.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_epoch", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("source_peer_type", sa.String(length=32), nullable=False),
            sa.Column("source_peer_id", sa.String(length=120), nullable=False),
            sa.Column("start_ingress_order", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="initializing"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("task_id", "task_epoch", name="uq_auth_update_sub_task_epoch"),
        )
        op.create_index("ix_auth_update_sub_state", "telegram_authorization_update_subscriptions", ["authorization_update_state_id", "state"])


def _upgrade_telegram_authorization_update_deliveries(existing_tables: set[str]) -> None:
    # 4. telegram_authorization_update_deliveries
    if "telegram_authorization_update_deliveries" not in existing_tables:
        op.create_table(
            "telegram_authorization_update_deliveries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("update_event_id", sa.String(length=36), sa.ForeignKey("telegram_authorization_update_events.id", ondelete="CASCADE"), nullable=False),
            sa.Column("subscription_id", sa.String(length=36), sa.ForeignKey("telegram_authorization_update_subscriptions.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(length=36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
            sa.Column("normalized_item_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("normalized_payload", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("payload_fingerprint", sa.String(length=64), nullable=False),
            sa.Column("delivery_state", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("update_event_id", "subscription_id", "normalized_item_index", name="uq_auth_update_delivery_item"),
        )
        op.create_index("ix_auth_update_delivery_task_state", "telegram_authorization_update_deliveries", ["task_id", "delivery_state"])


def _upgrade_telegram_outbound_random_id_mappings(existing_tables: set[str]) -> None:
    # 5. telegram_outbound_random_id_mappings
    if "telegram_outbound_random_id_mappings" not in existing_tables:
        op.create_table(
            "telegram_outbound_random_id_mappings",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("authorization_update_state_id", sa.String(length=36), sa.ForeignKey("telegram_authorization_update_states.id", ondelete="CASCADE"), nullable=False),
            sa.Column("gateway_mutation_identity_id", sa.String(length=36), nullable=True),
            sa.Column("random_id", sa.BigInteger(), nullable=False),
            sa.Column("gateway_request_journal_id", sa.String(length=36), nullable=True),
            sa.Column("action_id", sa.String(length=36), nullable=True),
            sa.Column("execution_attempt_id", sa.String(length=36), nullable=True),
            sa.Column("target_peer_type", sa.String(length=32), nullable=False),
            sa.Column("target_peer_id", sa.String(length=120), nullable=False),
            sa.Column("remote_message_or_topic_id", sa.String(length=120), nullable=False),
            sa.Column("update_identity_hash", sa.String(length=64), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("authorization_update_state_id", "random_id", name="uq_outbound_random_id_mapping"),
        )
        op.create_index("ix_outbound_random_id_attempt", "telegram_outbound_random_id_mappings", ["action_id", "execution_attempt_id"])
        op.create_index(
            "uq_outbound_random_id_gateway_request",
            "telegram_outbound_random_id_mappings",
            ["gateway_request_journal_id"],
            unique=True,
            postgresql_where=sa.text("gateway_request_journal_id IS NOT NULL"),
            sqlite_where=sa.text("gateway_request_journal_id IS NOT NULL"),
        )


def _upgrade_telegram_group_mutation_authorities(existing_tables: set[str]) -> None:
    # 6. telegram_group_mutation_authorities
    if "telegram_group_mutation_authorities" not in existing_tables:
        op.create_table(
            "telegram_group_mutation_authorities",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("target_peer_type", sa.String(length=32), nullable=False),
            sa.Column("target_peer_id", sa.String(length=120), nullable=False),
            sa.Column("mode", sa.String(length=32), nullable=False, server_default="shared"),
            sa.Column("cutover_generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("gateway_admission_side", sa.String(length=32), nullable=False, server_default="all"),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "target_peer_type", "target_peer_id", name="uq_group_mutation_auth_target"),
        )
        op.create_index("ix_group_mutation_auth_mode", "telegram_group_mutation_authorities", ["mode", "state"])


def _upgrade_telegram_group_mutation_authority_holders(existing_tables: set[str]) -> None:
    # 7. telegram_group_mutation_authority_holders
    if "telegram_group_mutation_authority_holders" not in existing_tables:
        op.create_table(
            "telegram_group_mutation_authority_holders",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("authority_id", sa.String(length=36), sa.ForeignKey("telegram_group_mutation_authorities.id", ondelete="CASCADE"), nullable=False),
            sa.Column("writer_kind", sa.String(length=32), nullable=False),
            sa.Column("writer_id", sa.String(length=64), nullable=False),
            sa.Column("route_hash", sa.String(length=64), nullable=False),
            sa.Column("holder_role", sa.String(length=32), nullable=False, server_default="primary"),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("authority_id", "writer_kind", "writer_id", "route_hash", name="uq_group_mutation_auth_holder"),
        )
        op.create_index("ix_group_mutation_auth_holder_writer", "telegram_group_mutation_authority_holders", ["writer_kind", "writer_id"])


def _upgrade_telegram_authorization_transport_states(existing_tables: set[str]) -> None:
    # 8. telegram_authorization_transport_states
    if "telegram_authorization_transport_states" not in existing_tables:
        op.create_table(
            "telegram_authorization_transport_states",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("authorization_id", sa.Integer(), sa.ForeignKey("tg_account_authorizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("session_generation", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("scope_type", sa.String(length=32), nullable=False, server_default="global"),
            sa.Column("target_peer_key", sa.String(length=120), nullable=False, server_default="*"),
            sa.Column("blocked_until", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reason", sa.String(length=80), nullable=False, server_default="flood_wait"),
            sa.Column("source_attempt_id", sa.String(length=36), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.UniqueConstraint("tenant_id", "authorization_id", "session_generation", "scope_type", "target_peer_key", name="uq_auth_transport_scope"),
        )
        op.create_index("ix_auth_transport_blocked", "telegram_authorization_transport_states", ["blocked_until"])


__all__ = ["upgrade_platform_tables"]
