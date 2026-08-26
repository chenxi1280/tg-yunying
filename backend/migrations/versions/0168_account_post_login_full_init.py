"""Add durable post-login full initialization.

Revision ID: 0168_post_login_full_init
Revises: 0167_legacy_ai_attempt
"""

from alembic import op
import sqlalchemy as sa


revision = "0168_post_login_full_init"
down_revision = "0167_legacy_ai_attempt"
branch_labels = None
depends_on = None


ACTIVE_FULL_INIT_SQL = (
    "status NOT IN ('succeeded','failed','manual_required','reconcile_unknown','cancelled')"
)


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _column_names(table_name: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _index_names(table_name: str) -> set[str]:
    return {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def _has_post_init_foreign_key() -> bool:
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys(
        "tg_account_login_batch_items"
    )
    return any(
        foreign_key["constrained_columns"] == ["post_initialization_id"]
        and foreign_key["referred_table"] == "tg_account_full_initializations"
        and foreign_key["referred_columns"] == ["id"]
        for foreign_key in foreign_keys
    )


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name in _column_names(table_name):
        return
    op.add_column(table_name, column)


def _create_index_if_missing(
    name: str,
    table_name: str,
    columns: list[str],
    **kwargs,
) -> None:
    if name in _index_names(table_name):
        return
    op.create_index(name, table_name, columns, **kwargs)


def _add_projection_columns() -> None:
    _add_column_if_missing(
        "tenants",
        sa.Column("fixed_two_fa_password_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        "UPDATE tenants SET fixed_two_fa_password_version = 1 "
        "WHERE fixed_two_fa_password_ciphertext <> ''"
    )
    for name, type_, default in (
        ("two_fa_password_source", sa.String(40), "legacy_unproven"),
        ("fixed_two_fa_version", sa.Integer(), "0"),
        ("two_fa_evidence_ref", sa.String(160), ""),
        ("two_fa_authorization_generation", sa.Integer(), "0"),
    ):
        _add_column_if_missing(
            "tg_account_security_snapshots",
            sa.Column(name, type_, nullable=False, server_default=default),
        )
    for name, type_, default in (
        ("authorized_count", sa.Integer(), "0"),
        ("fully_initialized_count", sa.Integer(), "0"),
        ("post_init_waiting_count", sa.Integer(), "0"),
        ("manual_required_count", sa.Integer(), "0"),
        ("initialization_policy", sa.String(48), "legacy_login_only"),
    ):
        _add_column_if_missing(
            "tg_account_login_batches",
            sa.Column(name, type_, nullable=False, server_default=default),
        )


def _create_full_initializations() -> None:
    if _has_table("tg_account_full_initializations"):
        return
    op.create_table(
        "tg_account_full_initializations",
        *_full_init_identity_columns(),
        *_full_init_stage_columns(),
        sa.UniqueConstraint("account_id", "generation", name="uq_account_full_init_generation"),
    )


def _full_init_identity_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("policy_version", sa.String(48), nullable=False, server_default="normal_full_init_v1"),
        sa.Column(
            "predecessor_initialization_id",
            sa.Integer(),
            sa.ForeignKey("tg_account_full_initializations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("target_pool_id", sa.Integer(), sa.ForeignKey("account_pools.id"), nullable=False),
        sa.Column("profile_policy_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(40), nullable=False, server_default="pending"),
        sa.Column("stage", sa.String(40), nullable=False, server_default="two_fa"),
        sa.Column("authorization_generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("fixed_two_fa_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_two_fa_kind", sa.String(40), nullable=False, server_default="unknown"),
        sa.Column("source_two_fa_password_ciphertext", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_secret_expires_at", sa.DateTime(), nullable=True),
        sa.Column("two_fa_status", sa.String(40), nullable=False, server_default="pending"),
        sa.Column("two_fa_call_state", sa.String(20), nullable=False, server_default="none"),
        sa.Column("two_fa_request_key", sa.String(100), nullable=False, server_default=""),
        sa.Column("two_fa_evidence_ref", sa.String(160), nullable=False, server_default=""),
        sa.Column("profile_status", sa.String(40), nullable=False, server_default="pending"),
        sa.Column(
            "profile_batch_id",
            sa.Integer(),
            sa.ForeignKey("tg_account_security_batches.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("profile_evidence_ref", sa.String(160), nullable=False, server_default=""),
        sa.Column(
            "profile_item_id",
            sa.Integer(),
            sa.ForeignKey("tg_account_security_batch_items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("profile_action_types", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("profile_target_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("profile_target_avatar_source", sa.String(300), nullable=False, server_default=""),
        sa.Column("profile_target_avatar_object_key", sa.String(500), nullable=False, server_default=""),
    )


def _full_init_stage_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("abc_status", sa.String(40), nullable=False, server_default="required"),
        sa.Column("abc_batch_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("abc_evidence_ref", sa.String(160), nullable=False, server_default=""),
        sa.Column("failure_type", sa.String(100), nullable=False, server_default=""),
        sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("originating_actor", sa.String(100), nullable=False, server_default=""),
        sa.Column(
            "execution_owner",
            sa.String(100),
            nullable=False,
            server_default="account-post-login-init-worker",
        ),
        sa.Column("lease_token", sa.String(80), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def _create_full_initialization_indexes() -> None:
    _create_index_if_missing(
        "ux_account_full_init_active",
        "tg_account_full_initializations",
        ["account_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_FULL_INIT_SQL),
        sqlite_where=sa.text(ACTIVE_FULL_INIT_SQL),
    )
    _create_index_if_missing(
        "ix_account_full_init_due",
        "tg_account_full_initializations",
        ["status", "next_retry_at", "id"],
    )


def _create_bindings_and_requests() -> None:
    _create_bindings()
    _create_requests()


def _create_bindings() -> None:
    if not _has_table("tg_account_login_post_init_bindings"):
        op.create_table(
            "tg_account_login_post_init_bindings",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("login_item_id", sa.Integer(), sa.ForeignKey("tg_account_login_batch_items.id"), nullable=False),
            sa.Column("login_execution_generation", sa.Integer(), nullable=False),
            sa.Column("full_initialization_id", sa.Integer(), sa.ForeignKey("tg_account_full_initializations.id"), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="attached"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("detached_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint(
                "login_item_id",
                "login_execution_generation",
                name="uq_login_post_init_binding_generation",
            ),
        )
    _create_index_if_missing(
        "ix_login_post_init_binding_owner",
        "tg_account_login_post_init_bindings",
        ["full_initialization_id", "status"],
    )


def _create_requests() -> None:
    if not _has_table("tg_post_login_abc_requests"):
        op.create_table(
            "tg_post_login_abc_requests",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id"), nullable=False),
            sa.Column("account_id", sa.Integer(), sa.ForeignKey("tg_accounts.id"), nullable=False),
            sa.Column("full_initialization_id", sa.Integer(), sa.ForeignKey("tg_account_full_initializations.id"), nullable=False),
            sa.Column("status", sa.String(40), nullable=False, server_default="waiting_approval"),
            sa.Column("request_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("requested_by", sa.String(100), nullable=False, server_default=""),
            sa.Column("approved_by", sa.String(100), nullable=False, server_default=""),
            sa.Column("approval_ref", sa.String(160), nullable=False, server_default=""),
            sa.Column("deployed_release_sha", sa.String(64), nullable=False, server_default=""),
            sa.Column("preview_fingerprint", sa.String(64), nullable=False, server_default=""),
            sa.Column("abc_batch_id", sa.String(36), nullable=False, server_default=""),
            sa.Column("failure_type", sa.String(100), nullable=False, server_default=""),
            sa.Column("failure_detail", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("approved_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("full_initialization_id", name="uq_post_login_abc_full_init"),
        )
    _create_index_if_missing(
        "ix_post_login_abc_request_status",
        "tg_post_login_abc_requests",
        ["tenant_id", "status", "created_at"],
    )


def _add_item_projection() -> None:
    for name, type_, default in (
        ("initialization_policy", sa.String(48), "legacy_login_only"),
        ("authorization_status", sa.String(40), "not_confirmed"),
        ("post_initialization_status", sa.String(40), "not_requested"),
        ("post_initialization_failure_type", sa.String(100), ""),
    ):
        _add_column_if_missing(
            "tg_account_login_batch_items",
            sa.Column(name, type_, nullable=False, server_default=default),
        )
    _add_column_if_missing(
        "tg_account_login_batch_items",
        sa.Column("post_initialization_id", sa.Integer(), nullable=True),
    )
    if not _has_post_init_foreign_key():
        op.create_foreign_key(
            "fk_login_item_post_initialization",
            "tg_account_login_batch_items",
            "tg_account_full_initializations",
            ["post_initialization_id"],
            ["id"],
            ondelete="SET NULL",
        )
    _create_index_if_missing(
        "ux_login_batch_item_account",
        "tg_account_login_batch_items",
        ["batch_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("account_id IS NOT NULL"),
        sqlite_where=sa.text("account_id IS NOT NULL"),
    )


def upgrade() -> None:
    _add_projection_columns()
    _create_full_initializations()
    _create_full_initialization_indexes()
    _create_bindings_and_requests()
    _add_item_projection()


def downgrade() -> None:
    op.drop_index(
        "ux_login_batch_item_account",
        table_name="tg_account_login_batch_items",
    )
    op.drop_constraint(
        "fk_login_item_post_initialization",
        "tg_account_login_batch_items",
        type_="foreignkey",
    )
    for name in (
        "post_initialization_id",
        "post_initialization_failure_type",
        "post_initialization_status",
        "authorization_status",
        "initialization_policy",
    ):
        op.drop_column("tg_account_login_batch_items", name)
    op.drop_index("ix_post_login_abc_request_status", table_name="tg_post_login_abc_requests")
    op.drop_table("tg_post_login_abc_requests")
    op.drop_index("ix_login_post_init_binding_owner", table_name="tg_account_login_post_init_bindings")
    op.drop_table("tg_account_login_post_init_bindings")
    op.drop_index("ix_account_full_init_due", table_name="tg_account_full_initializations")
    op.drop_index("ux_account_full_init_active", table_name="tg_account_full_initializations")
    op.drop_table("tg_account_full_initializations")
    for name in (
        "initialization_policy",
        "manual_required_count",
        "post_init_waiting_count",
        "fully_initialized_count",
        "authorized_count",
    ):
        op.drop_column("tg_account_login_batches", name)
    for name in (
        "two_fa_authorization_generation",
        "two_fa_evidence_ref",
        "fixed_two_fa_version",
        "two_fa_password_source",
    ):
        op.drop_column("tg_account_security_snapshots", name)
    op.drop_column("tenants", "fixed_two_fa_password_version")
