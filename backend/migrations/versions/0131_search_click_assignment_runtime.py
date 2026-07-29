"""Add pure search-click assignment and release facts.

Revision ID: 0131_search_click_runtime
Revises: 0130_fulfillment_runtime
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

from app.database import Base
from app import models  # noqa: F401


revision = "0131_search_click_runtime"
down_revision = "0130_fulfillment_runtime"
branch_labels = None
depends_on = None


SEARCH_CLICK_TABLES = (
    "search_click_task_fairness_states",
    "search_click_assignment_epochs",
    "search_click_solver_problem_snapshots",
    "search_click_solver_problem_components",
    "search_click_solver_carrier_unit_bindings",
    "search_click_opportunity_assignments",
    "dispatch_allocation_release_batches",
    "dispatch_allocation_release_batch_items",
    "dispatch_allocation_exclusions",
)


def upgrade() -> None:
    for table_name in SEARCH_CLICK_TABLES:
        Base.metadata.tables[table_name].create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("0131 search-click migration is forward-only")
