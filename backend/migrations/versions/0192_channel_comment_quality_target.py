"""Add append-only channel comment quality target revisions.

Revision ID: 0192_comment_quality_target
Revises: 0191_comment_source_delete
"""

from __future__ import annotations

import hashlib
import json
import math
from uuid import NAMESPACE_URL, uuid5

from alembic import context, op
import sqlalchemy as sa


revision = "0192_comment_quality_target"
down_revision = "0191_comment_source_delete"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not _has_table("channel_comment_quality_target_revisions"):
        _create_quality_target_table()
    _add_columns()
    _backfill_quality_targets()
    _add_foreign_keys()


def _create_quality_target_table() -> None:
    op.create_table(
        "channel_comment_quality_target_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.Integer(), nullable=False),
        sa.Column("plan_contract_id", sa.String(length=36), nullable=False),
        sa.Column("quality_target_revision", sa.Integer(), nullable=False),
        sa.Column("supersedes_quality_target_revision_id", sa.String(length=36), nullable=True),
        sa.Column("component_targets_json", sa.JSON(), nullable=False),
        sa.Column("aggregate_grounding_required_count", sa.Integer(), nullable=False),
        sa.Column("aggregate_planned_fallback_count", sa.Integer(), nullable=False),
        sa.Column("component_set_hash", sa.String(length=64), nullable=False),
        sa.Column("target_state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plan_contract_id"], ["channel_comment_plan_contracts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_quality_target_revision_id"],
            ["channel_comment_quality_target_revisions.id"],
            name="fk_channel_comment_quality_target_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plan_contract_id", "quality_target_revision",
            name="uq_channel_comment_quality_target_revision",
        ),
    )


def _add_columns() -> None:
    if not _has_column("channel_comment_plan_contracts", "initial_quality_target_revision_id"):
        op.add_column(
            "channel_comment_plan_contracts",
            sa.Column("initial_quality_target_revision_id", sa.String(length=36), nullable=True),
        )
    if not _has_column("channel_comment_plan_contracts", "current_quality_target_revision_id"):
        op.add_column(
            "channel_comment_plan_contracts",
            sa.Column("current_quality_target_revision_id", sa.String(length=36), nullable=True),
        )
    if not _has_column("channel_comment_grounding_assignments", "quality_target_revision_id"):
        op.add_column(
            "channel_comment_grounding_assignments",
            sa.Column("quality_target_revision_id", sa.String(length=36), nullable=True),
        )
    if not _has_column("channel_comment_grounding_assignments", "quality_component_key"):
        op.add_column(
            "channel_comment_grounding_assignments",
            sa.Column("quality_component_key", sa.String(length=64), nullable=False, server_default=""),
        )


def _backfill_quality_targets() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    plans = sa.Table("channel_comment_plan_contracts", metadata, autoload_with=bind)
    targets = sa.Table("channel_comment_quality_target_revisions", metadata, autoload_with=bind)
    assignments = sa.Table("channel_comment_grounding_assignments", metadata, autoload_with=bind)
    rows = bind.execute(sa.select(plans).where(
        plans.c.current_quality_target_revision_id.is_(None),
    )).mappings()
    for plan in rows:
        target_id, component = _backfill_component(bind, assignments, plan)
        bind.execute(targets.insert().values(
            id=target_id,
            tenant_id=plan["tenant_id"],
            plan_contract_id=plan["id"],
            quality_target_revision=1,
            supersedes_quality_target_revision_id=None,
            component_targets_json=[component],
            aggregate_grounding_required_count=component["grounding_required_count"],
            aggregate_planned_fallback_count=component["planned_fallback_count"],
            component_set_hash=_hash([component]),
            target_state="frozen",
            created_at=plan["created_at"],
        ))
        bind.execute(plans.update().where(plans.c.id == plan["id"]).values(
            initial_quality_target_revision_id=target_id,
            current_quality_target_revision_id=target_id,
        ))
        bind.execute(assignments.update().where(
            assignments.c.plan_contract_id == plan["id"],
        ).values(
            quality_target_revision_id=target_id,
            quality_component_key=component["quality_component_key"],
        ))


def _backfill_component(bind, assignments, plan: dict) -> tuple[str, dict]:
    assignment_rows = list(bind.execute(sa.select(
        assignments.c.target_ordinal,
        assignments.c.teacher_name,
        assignments.c.primary_aspect_code,
    ).where(
        assignments.c.plan_contract_id == plan["id"],
        assignments.c.assignment_state == "active",
    )).mappings())
    owned = list(range(1, int(plan["required_distinct_account_count"]) + 1))
    grounding = _grounding_ordinals(plan, assignment_rows, owned)
    raw_count = math.ceil(len(owned) * 8500 / 10000)
    capacity_count = int(plan["grounding_required_count"])
    component = {
        "comment_grounding_revision": 1,
        "source_revision_id": plan["source_revision_id"],
        "owned_ordinal_ids": owned,
        "raw_grounding_ordinal_ids": owned[:raw_count],
        "groundable_ordinal_ids": owned[:capacity_count],
        "grounding_ordinal_ids": grounding,
        "planned_fallback_ordinal_ids": [value for value in owned if value not in grounding],
        "teacher_binding_ordinal_ids": sorted({
            int(row["target_ordinal"]) for row in assignment_rows if row["teacher_name"]
        }),
        "primary_aspect_by_ordinal": {
            str(row["target_ordinal"]): str(row["primary_aspect_code"])
            for row in assignment_rows if row["primary_aspect_code"]
        },
        "semantic_capacity_policy_version": "legacy_plan_count_backfill_v1",
        "semantic_capacity_result_hash": _hash({
            "plan_id": plan["id"], "grounding_required_count": capacity_count,
        }),
    }
    component = _finalize_component(component)
    target_id = str(uuid5(NAMESPACE_URL, f"channel-comment-quality-target:{plan['id']}:1"))
    return target_id, component


def _grounding_ordinals(plan: dict, assignments: list[dict], owned: list[int]) -> list[int]:
    required = int(plan["grounding_required_count"])
    values = sorted({int(row["target_ordinal"]) for row in assignments})[:required]
    for ordinal in owned:
        if len(values) >= required:
            break
        if ordinal not in values:
            values.append(ordinal)
    return sorted(values)


def _finalize_component(component: dict) -> dict:
    owned = component["owned_ordinal_ids"]
    raw = component["raw_grounding_ordinal_ids"]
    groundable = component["groundable_ordinal_ids"]
    grounding = component["grounding_ordinal_ids"]
    fallback = component["planned_fallback_ordinal_ids"]
    component.update({
        "owned_ordinal_count": len(owned),
        "owned_ordinal_ids_hash": _hash(owned),
        "unadjusted_grounding_target_count": len(raw),
        "groundable_capacity_count": len(groundable),
        "grounding_required_count": len(grounding),
        "planned_fallback_count": len(fallback),
        "teacher_binding_required_count": len(component["teacher_binding_ordinal_ids"]),
        "primary_aspect_required_distinct_count": len(set(
            component["primary_aspect_by_ordinal"].values(),
        )),
        "semantic_capacity_state": _capacity_state(len(raw), len(groundable)),
    })
    component["quality_component_key"] = _hash({
        "grounding_revision": component["comment_grounding_revision"],
        "source_revision_id": component["source_revision_id"],
        "owned_ordinal_ids_hash": component["owned_ordinal_ids_hash"],
    })
    return component


def _add_foreign_keys() -> None:
    _create_fk(
        "channel_comment_plan_contracts",
        "fk_channel_comment_plan_initial_quality_target",
        ["initial_quality_target_revision_id"],
    )
    _create_fk(
        "channel_comment_plan_contracts",
        "fk_channel_comment_plan_current_quality_target",
        ["current_quality_target_revision_id"],
    )
    _create_fk(
        "channel_comment_grounding_assignments",
        "fk_channel_comment_assignment_quality_target",
        ["quality_target_revision_id"],
    )


def _create_fk(table_name: str, name: str, columns: list[str]) -> None:
    if _has_foreign_key(table_name, columns):
        return
    op.create_foreign_key(
        name,
        table_name,
        "channel_comment_quality_target_revisions",
        columns,
        ["id"],
        ondelete="RESTRICT",
    )


def _has_table(table_name: str) -> bool:
    if context.is_offline_mode():
        return False
    return table_name in sa.inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    if context.is_offline_mode() or not _has_table(table_name):
        return False
    return any(
        row["name"] == column_name
        for row in sa.inspect(op.get_bind()).get_columns(table_name)
    )


def _has_foreign_key(table_name: str, columns: list[str]) -> bool:
    if context.is_offline_mode() or not _has_table(table_name):
        return False
    return any(
        row["constrained_columns"] == columns
        for row in sa.inspect(op.get_bind()).get_foreign_keys(table_name)
    )


def _capacity_state(raw_count: int, capacity_count: int) -> str:
    if capacity_count == 0:
        return "none"
    return "sufficient" if capacity_count >= raw_count else "capacity_adjusted"


def _hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def downgrade() -> None:
    op.drop_constraint(
        "fk_channel_comment_assignment_quality_target",
        "channel_comment_grounding_assignments",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_channel_comment_plan_current_quality_target",
        "channel_comment_plan_contracts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_channel_comment_plan_initial_quality_target",
        "channel_comment_plan_contracts",
        type_="foreignkey",
    )
    op.drop_column("channel_comment_grounding_assignments", "quality_component_key")
    op.drop_column("channel_comment_grounding_assignments", "quality_target_revision_id")
    op.drop_column("channel_comment_plan_contracts", "current_quality_target_revision_id")
    op.drop_column("channel_comment_plan_contracts", "initial_quality_target_revision_id")
    op.drop_table("channel_comment_quality_target_revisions")
