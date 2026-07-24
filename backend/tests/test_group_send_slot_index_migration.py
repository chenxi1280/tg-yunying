from pathlib import Path

import pytest


pytestmark = pytest.mark.no_postgres


def test_group_send_slot_index_migration_supports_indexed_group_lookup() -> None:
    migrations = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    migration = migrations / "0113_group_send_slot_lookup_index.py"

    assert migration.exists()
    source = migration.read_text()
    assert "ix_actions_send_group_slot_lookup" in source
    assert "CREATE INDEX CONCURRENTLY" in source
    assert "payload ->> 'group_id'" in source
