from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.models.channel_comment_discussion import (
    ChannelDiscussionThreadBinding,
    DiscussionMembershipFact,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def test_alembic_versions_have_single_head():
    versions_dir = PROJECT_ROOT / "backend/migrations/versions"
    revisions: dict[str, str | tuple[str, ...] | None] = {}
    for migration in versions_dir.glob("*.py"):
        module = ast.parse(migration.read_text())
        values = {
            node.targets[0].id: ast.literal_eval(node.value)
            for node in module.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"revision", "down_revision"}
        }
        if values.get("revision"):
            revisions[str(values["revision"])] = values.get("down_revision")

    referenced = set()
    for down_revision in revisions.values():
        if isinstance(down_revision, str):
            referenced.add(down_revision)
        elif isinstance(down_revision, tuple):
            referenced.update(item for item in down_revision if item)

    assert all(len(revision) <= 32 for revision in revisions)
    heads = sorted(set(revisions) - referenced)
    assert heads == ["0225_account_group_revisions"]



def test_backend_test_names_are_unique_per_file():
    for path in (PROJECT_ROOT / "backend/tests").glob("test_*.py"):
        module = ast.parse(path.read_text())
        names = [
            node.name
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        assert len(names) == len(set(names)), path


def test_backend_scripts_compile() -> None:
    for path in (PROJECT_ROOT / "backend/scripts").glob("*.py"):
        compile(path.read_text(), str(path), "exec")


def test_channel_comment_cycle_foreign_keys_keep_explicit_alter_metadata() -> None:
    columns = (
        ChannelDiscussionThreadBinding.__table__.c.probe_event_id,
        ChannelDiscussionThreadBinding.__table__.c.supersedes_thread_binding_id,
        DiscussionMembershipFact.__table__.c.supersedes_fact_id,
    )
    foreign_keys = [next(iter(column.foreign_keys)) for column in columns]

    assert [foreign_key.use_alter for foreign_key in foreign_keys] == [True, True, True]
    assert [foreign_key.name for foreign_key in foreign_keys] == [
        "fk_thread_binding_probe_event",
        "fk_thread_binding_supersedes",
        "fk_disc_membership_supersedes",
    ]


def test_task_center_timeout_constant_is_declared_once():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    assert source.count("const TASK_CREATE_TIMEOUT_MS = 120_000") == 1
