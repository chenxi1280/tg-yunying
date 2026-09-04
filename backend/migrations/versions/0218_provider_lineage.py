"""Index stable generation lineage without changing historical call evidence."""
from alembic import op


revision = "0218_provider_lineage"
down_revision = "0217_provider_http_exchanges"
branch_labels = None
depends_on = None

INDEX = "ix_generation_job_provider_lineage"


def upgrade():
    op.create_index(INDEX, "generation_jobs", ["tenant_id", "task_id", "obligation_type", "obligation_id"])


def downgrade():
    op.drop_index(INDEX, table_name="generation_jobs")
