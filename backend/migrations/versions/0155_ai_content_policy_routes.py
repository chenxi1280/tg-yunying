"""Add AI content policy, context revision, and provider route-set schema.

Revision ID: 0155_ai_content_policy_routes
Revises: 0154_account_pacing_action_state_index
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "0155_ai_content_policy_routes"
down_revision = "0154_account_pacing_action_state"
branch_labels = None
depends_on = None
LEGACY_GROUP_PURPOSES = (
    "group_context_route",
    "group_realize_general",
    "group_realize_adult_visual",
    "group_realize_adult_product",
    "group_realize_adult_service_inquiry",
    "group_realize_adult_service_sensory",
    "group_semantic_review",
)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _has_column(table: str, column: str) -> bool:
    if not _has_table(table):
        return False
    return column in {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(table)}


def _add_credential_enabled() -> None:
    if _has_column("ai_providers", "credential_enabled"):
        return
    op.add_column(
        "ai_providers",
        sa.Column("credential_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(sa.text(
        "UPDATE ai_providers SET credential_enabled = true "
        "WHERE is_active = true OR id IN ("
        "SELECT default_provider_id FROM tenant_ai_settings WHERE default_provider_id IS NOT NULL)"
    ))


def _create_policy_versions() -> None:
    if _has_table("ai_content_policy_versions"):
        return
    op.create_table(
        "ai_content_policy_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("route_rules", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("prompt_registry", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("gate_config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("example_set", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
        sa.Column("approved_by", sa.String(160), nullable=False, server_default=""),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "version", name="uq_ai_content_policy_tenant_version"),
    )
    op.create_index(
        "uq_ai_content_policy_active",
        "ai_content_policy_versions",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def _create_attestations() -> None:
    if _has_table("adult_subject_attestations"):
        return
    op.create_table(
        "adult_subject_attestations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope_type", sa.String(24), nullable=False),
        sa.Column("scope_id", sa.String(160), nullable=False),
        sa.Column("subject_class", sa.String(48), nullable=False),
        sa.Column("evidence_codes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("actor_user_id", sa.Integer(), sa.ForeignKey("app_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("permission_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("task_config_revision", sa.Integer(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.CheckConstraint("scope_type IN ('task_group','task_source')", name="ck_adult_attestation_scope_type"),
    )
    op.create_index(
        "ix_adult_attestation_scope",
        "adult_subject_attestations",
        ["tenant_id", "scope_type", "scope_id", "status", "expires_at"],
    )


def _create_bindings() -> None:
    if _has_table("task_ai_content_policy_bindings"):
        return
    op.create_table(
        "task_ai_content_policy_bindings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("task_config_revision", sa.Integer(), nullable=False),
        sa.Column("policy_version_id", sa.String(36), sa.ForeignKey("ai_content_policy_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("allowed_routes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("attestation_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("style_overlay_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("approved_by", sa.String(160), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "task_config_revision",
            name="uq_task_ai_content_policy_revision",
        ),
    )


def _create_context_revisions() -> None:
    if _has_table("context_scope_revisions"):
        return
    op.create_table(
        "context_scope_revisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scope_type", sa.String(24), nullable=False),
        sa.Column("scope_id", sa.String(160), nullable=False),
        sa.Column("context_scope_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("context_snapshot_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_human_message_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("reply_target_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("scope_type IN ('group','comment_source')", name="ck_context_scope_revision_type"),
        sa.UniqueConstraint("tenant_id", "scope_type", "scope_id", name="uq_context_scope_revision_scope"),
    )


def _create_provider_routes() -> None:
    if not _has_table("tenant_ai_provider_route_sets"):
        op.create_table(
            "tenant_ai_provider_route_sets",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
            sa.Column("purpose", sa.String(64), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("approved_by", sa.String(160), nullable=False, server_default=""),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.UniqueConstraint("tenant_id", "purpose", "revision", name="uq_tenant_ai_provider_route_revision"),
        )
        op.create_index(
            "uq_tenant_ai_provider_route_active",
            "tenant_ai_provider_route_sets",
            ["tenant_id", "purpose"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )
    if _has_table("tenant_ai_provider_route_items"):
        return
    op.create_table(
        "tenant_ai_provider_route_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("route_set_id", sa.String(36), sa.ForeignKey("tenant_ai_provider_route_sets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("timeout_ms", sa.Integer(), nullable=False, server_default="30000"),
        sa.Column("rate_policy", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("concurrency_policy", sa.JSON(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("route_set_id", "priority", name="uq_tenant_ai_route_item_priority"),
    )


def _backfill_legacy_provider_routes() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(
        "SELECT settings.tenant_id, providers.id, providers.model_name "
        "FROM tenant_ai_settings AS settings "
        "JOIN ai_providers AS providers ON providers.id = settings.default_provider_id "
        "WHERE providers.credential_enabled = true"
    )).all()
    for tenant_id, provider_id, model_name in rows:
        for purpose in LEGACY_GROUP_PURPOSES:
            _backfill_legacy_route(bind, tenant_id, provider_id, model_name, purpose)


def _backfill_legacy_route(bind, tenant_id: int, provider_id: int, model_name: str, purpose: str) -> None:
    existing = bind.scalar(sa.text(
        "SELECT id FROM tenant_ai_provider_route_sets "
        "WHERE tenant_id = :tenant_id AND purpose = :purpose LIMIT 1"
    ), {"tenant_id": tenant_id, "purpose": purpose})
    if existing:
        return
    route_set_id = str(uuid4())
    content_hash = _legacy_route_hash(tenant_id, provider_id, model_name, purpose)
    bind.execute(sa.text(
        "INSERT INTO tenant_ai_provider_route_sets "
        "(id, tenant_id, purpose, revision, status, content_hash, approved_by, approved_at) "
        "VALUES (:id, :tenant_id, :purpose, 1, 'active', :content_hash, "
        "'migration:0155', CURRENT_TIMESTAMP)"
    ), {
        "id": route_set_id,
        "tenant_id": tenant_id,
        "purpose": purpose,
        "content_hash": content_hash,
    })
    bind.execute(sa.text(
        "INSERT INTO tenant_ai_provider_route_items "
        "(id, route_set_id, priority, provider_id, model_name, enabled) "
        "VALUES (:id, :route_set_id, 1, :provider_id, :model_name, true)"
    ), {
        "id": str(uuid4()),
        "route_set_id": route_set_id,
        "provider_id": provider_id,
        "model_name": model_name,
    })


def _legacy_route_hash(tenant_id: int, provider_id: int, model_name: str, purpose: str) -> str:
    payload = json.dumps({
        "tenant_id": tenant_id,
        "purpose": purpose,
        "revision": 1,
        "items": [{"priority": 1, "provider_id": provider_id, "model_name": model_name}],
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    _add_credential_enabled()
    _create_policy_versions()
    _create_attestations()
    _create_bindings()
    _create_context_revisions()
    _create_provider_routes()
    _backfill_legacy_provider_routes()


def downgrade() -> None:
    for table in (
        "tenant_ai_provider_route_items",
        "tenant_ai_provider_route_sets",
        "context_scope_revisions",
        "task_ai_content_policy_bindings",
        "adult_subject_attestations",
        "ai_content_policy_versions",
    ):
        if _has_table(table):
            op.drop_table(table)
    if _has_column("ai_providers", "credential_enabled"):
        op.drop_column("ai_providers", "credential_enabled")
