from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0171_phone_mask_display_index.py"
)


class FakeOperations:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, bool | None]] = []

    def drop_index(self, name: str, *, table_name: str) -> None:
        self.events.append(("drop", name, None))

    def create_index(self, name: str, table_name: str, columns, **options) -> None:
        self.events.append(("create", name, options.get("unique")))


def _migration_module():
    spec = importlib.util.spec_from_file_location("phone_mask_display_index", MIGRATION_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_replaces_masked_phone_unique_index(monkeypatch) -> None:
    migration = _migration_module()
    operations = FakeOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_index_names", lambda: {migration.OLD_INDEX})

    migration.upgrade()

    assert operations.events == [
        ("drop", migration.OLD_INDEX, None),
        ("create", migration.NEW_INDEX, False),
    ]


def test_downgrade_blocks_active_mask_collision(monkeypatch) -> None:
    migration = _migration_module()
    monkeypatch.setattr(migration, "op", FakeOperations())
    monkeypatch.setattr(migration, "_index_names", lambda: {migration.NEW_INDEX})
    monkeypatch.setattr(migration, "_has_active_mask_collision", lambda: True)

    with pytest.raises(RuntimeError, match="active collisions"):
        migration.downgrade()
