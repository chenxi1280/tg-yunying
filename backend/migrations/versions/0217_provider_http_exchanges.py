"""Record actual HTTP exchanges without fabricating historical calls or budget approval."""
from alembic import op
import sqlalchemy as sa

revision = "0217_provider_http_exchanges"
down_revision = "0216_generation_timing_binding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_http_exchanges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("chain_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), sa.ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("task_lifecycle_epoch", sa.Integer(), nullable=False),
        sa.Column("provider_id", sa.Integer(), sa.ForeignKey("ai_providers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("logical_request_id", sa.String(200), nullable=False),
        sa.Column("model_name", sa.String(120), nullable=False),
        sa.Column("purpose", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_hash", sa.String(64), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=False),
        sa.Column("local_termination_confirmed", sa.Boolean(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_provider_http_exchange_logical", "provider_http_exchanges", ["logical_request_id", "provider_id"])
    op.create_table(
        "provider_http_exchange_jobs",
        sa.Column("exchange_id", sa.String(36), sa.ForeignKey("provider_http_exchanges.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("generation_job_id", sa.String(36), sa.ForeignKey("generation_timing_bindings.generation_job_id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("execution_path_hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_provider_http_exchange_job", "provider_http_exchange_jobs", ["generation_job_id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM provider_http_exchanges")):
        raise RuntimeError("provider_http_downgrade_would_discard_execution_evidence")
    op.drop_table("provider_http_exchange_jobs")
    op.drop_table("provider_http_exchanges")
