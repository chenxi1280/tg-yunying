from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/migrations/versions/0112_task_scope_delete_cascade.py"


def test_scope_delete_cascade_migration_replaces_existing_postgres_foreign_keys(monkeypatch) -> None:
    migration = _migration_module()
    operation = _FakePostgresOp()
    migration.op = operation
    monkeypatch.setattr(migration.sa, "inspect", lambda _bind: _FakeInspector(migration.CASCADE_FOREIGN_KEYS))

    migration.upgrade()

    assert operation.dropped == [
        (f"legacy_{table_name}_{column_name}", table_name)
        for table_name, column_name, _target_table, _constraint_name in migration.CASCADE_FOREIGN_KEYS
    ]
    assert operation.created == [
        (constraint_name, table_name, target_table, [column_name], ["id"], {"ondelete": "CASCADE"})
        for table_name, column_name, target_table, constraint_name in migration.CASCADE_FOREIGN_KEYS
    ]


def test_scope_delete_cascade_migration_downgrade_removes_cascade(monkeypatch) -> None:
    migration = _migration_module()
    operation = _FakePostgresOp()
    migration.op = operation
    monkeypatch.setattr(
        migration.sa,
        "inspect",
        lambda _bind: _FakeInspector(migration.CASCADE_FOREIGN_KEYS, ondelete="CASCADE"),
    )

    migration.downgrade()

    assert operation.created == [
        (constraint_name, table_name, target_table, [column_name], ["id"], {})
        for table_name, column_name, target_table, constraint_name in migration.CASCADE_FOREIGN_KEYS
    ]


def _migration_module():
    spec = importlib.util.spec_from_file_location("task_scope_delete_cascade_0112", MIGRATION_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("migration module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakePostgresOp:
    def __init__(self) -> None:
        self.dropped: list[tuple[str, str]] = []
        self.created: list[tuple[str, str, str, list[str], list[str], dict]] = []

    def get_bind(self):
        return _FakeBind()

    def drop_constraint(self, name: str, table_name: str, *, type_: str) -> None:
        assert type_ == "foreignkey"
        self.dropped.append((name, table_name))

    def create_foreign_key(
        self,
        name: str,
        source_table: str,
        target_table: str,
        local_columns: list[str],
        remote_columns: list[str],
        **kwargs,
    ) -> None:
        self.created.append((name, source_table, target_table, local_columns, remote_columns, kwargs))


class _FakeBind:
    dialect = type("Dialect", (), {"name": "postgresql"})()


class _FakeInspector:
    def __init__(self, foreign_keys, *, ondelete: str | None = None) -> None:
        self._foreign_keys = foreign_keys
        self._ondelete = ondelete

    def get_table_names(self) -> list[str]:
        return list({table_name for table_name, _column_name, _target_table, _name in self._foreign_keys})

    def get_foreign_keys(self, table_name: str) -> list[dict]:
        return [
            {
                "name": f"legacy_{current_table}_{column_name}",
                "constrained_columns": [column_name],
                "referred_table": target_table,
                "options": {"ondelete": self._ondelete} if self._ondelete else {},
            }
            for current_table, column_name, target_table, _name in self._foreign_keys
            if current_table == table_name
        ]
