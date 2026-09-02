from pathlib import Path

import pytest

from app.database import Base


pytestmark = pytest.mark.no_postgres


EXPECTED_TABLES = {
    "channel_discussion_group_probe_events",
    "channel_discussion_group_bindings",
    "channel_discussion_thread_probe_events",
    "channel_discussion_thread_bindings",
    "discussion_membership_facts",
    "channel_comment_grounding_enrollments",
    "channel_comment_listener_error_events",
    "channel_comment_recovery_manifests",
}


def test_discussion_contract_metadata_contains_all_runtime_tables_and_close_boundary() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)
    enrollment = Base.metadata.tables["channel_comment_grounding_enrollments"]
    assert {"enabled_at", "closed_at", "activation_hash", "enrollment_state"} <= set(enrollment.columns.keys())
    membership = Base.metadata.tables["discussion_membership_facts"]
    assert {"membership_status", "can_send", "fresh_until_at", "is_current"} <= set(membership.columns.keys())


def test_0194_migration_is_single_successor_and_contains_all_tables() -> None:
    path = Path(__file__).resolve().parents[1] / "migrations/versions/0194_channel_comment_discussion_contracts.py"
    source = path.read_text()

    assert 'revision = "0194_channel_comment_discussion"' in source
    assert 'down_revision = "0193_comment_business_guards"' in source
    assert 'sa.Column("closed_at", sa.DateTime(timezone=True))' in source
    assert all(f'"{table}"' in source for table in EXPECTED_TABLES)
