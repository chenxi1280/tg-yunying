from __future__ import annotations

import ast
from pathlib import Path

import pytest


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
    assert heads == ["0093_runtime_stats_indexes"]


def test_backend_test_names_are_unique_per_file():
    for path in (PROJECT_ROOT / "backend/tests").glob("test_*.py"):
        module = ast.parse(path.read_text())
        names = [
            node.name
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
        ]
        assert len(names) == len(set(names)), path


def test_task_center_timeout_constant_is_declared_once():
    source = (PROJECT_ROOT / "frontend/src/app/views/TaskCenterView.tsx").read_text()
    assert source.count("const TASK_CREATE_TIMEOUT_MS = 120_000") == 1
