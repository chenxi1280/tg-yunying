"""Add group_clone v2 core tables and platform shared authority / ingress tables.

Revision ID: 0180_group_clone_v2_core
Revises: 0173_channel_view_fact_nav
"""

from alembic import op
from sqlalchemy import inspect

from app.migration_helpers.group_clone_v2_domain import upgrade_clone_tables
from app.migration_helpers.group_clone_v2_platform import upgrade_platform_tables


revision = "0180_group_clone_v2_core"
down_revision = "0173_channel_view_fact_nav"
branch_labels = None
depends_on = None


DROP_ORDER = (
    "clone_cutover_exclusions",
    "clone_sequencer_head_cases",
    "clone_manual_review_decisions",
    "clone_message_parts",
    "clone_delivery_obligations",
    "telegram_gateway_mutation_identities",
    "clone_topic_maps",
    "clone_album_items",
    "clone_album_manifests",
    "clone_sender_binding_history",
    "clone_account_slots",
    "clone_target_execution_snapshots",
    "clone_target_route_snapshots",
    "clone_source_events",
    "clone_source_stream_states",
    "telegram_authorization_transport_states",
    "telegram_group_mutation_authority_holders",
    "telegram_group_mutation_authorities",
    "telegram_outbound_random_id_mappings",
    "telegram_authorization_update_deliveries",
    "telegram_authorization_update_subscriptions",
    "telegram_authorization_update_events",
    "telegram_authorization_update_states",
)


def upgrade() -> None:
    existing_tables = set(inspect(op.get_bind()).get_table_names())
    upgrade_platform_tables(existing_tables)
    upgrade_clone_tables(existing_tables)


def downgrade() -> None:
    for table_name in DROP_ORDER:
        op.drop_table(table_name)

