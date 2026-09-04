from importlib import import_module

from sqlalchemy import create_engine


def test_peer_identity_backfill_preserves_resolvable_historical_comments() -> None:
    migration = import_module(
        "migrations.versions.0209_channel_comment_peer_identity"
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        _create_backfill_fixture(connection)
        migration._backfill_current_discussion_peers(connection)
        rows = connection.exec_driver_sql(
            "SELECT id, discussion_peer_id FROM channel_message_comments ORDER BY id"
        ).all()
    assert rows == [
        (1, "-1007001"),
        (2, ""),
        (3, "-1009001"),
        (4, ""),
    ]


def _create_backfill_fixture(connection) -> None:
    statements = (
        "CREATE TABLE channel_messages (id INTEGER PRIMARY KEY, current_source_revision_id TEXT)",
        "CREATE TABLE channel_message_source_revisions (id TEXT PRIMARY KEY, tenant_id INTEGER, channel_target_id INTEGER, channel_message_id INTEGER, discussion_thread_binding_id TEXT)",
        "CREATE TABLE channel_discussion_thread_bindings (id TEXT PRIMARY KEY, source_revision_id TEXT, discussion_peer_id TEXT, is_current BOOLEAN)",
        "CREATE TABLE channel_message_comments (id INTEGER PRIMARY KEY, tenant_id INTEGER, channel_target_id INTEGER, channel_message_id INTEGER, discussion_peer_id TEXT)",
        "INSERT INTO channel_messages VALUES (41, 'revision-1'), (42, NULL), (43, 'revision-2'), (44, 'revision-3')",
        "INSERT INTO channel_message_source_revisions VALUES ('revision-1', 1, 31, 41, 'thread-1'), ('revision-2', 1, 31, 43, 'thread-2'), ('revision-3', 1, 31, 44, 'thread-3'), ('revision-4', 1, 31, 44, 'thread-4')",
        "INSERT INTO channel_discussion_thread_bindings VALUES ('thread-1', 'revision-1', '-1007001', 1), ('thread-2', 'revision-2', '-1008001', 0), ('thread-3', 'revision-3', '-1008001', 1), ('thread-4', 'revision-4', '-1008002', 0)",
        "INSERT INTO channel_message_comments VALUES (1, 1, 31, 41, ''), (2, 1, 31, 42, ''), (3, 1, 31, 43, '-1009001'), (4, 1, 31, 44, '')",
    )
    for statement in statements:
        connection.exec_driver_sql(statement)
