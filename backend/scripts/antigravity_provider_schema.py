from __future__ import annotations

import sqlite3


REQUEST_COLUMNS = (
    "request_id", "request_hash", "state", "response_ciphertext", "error_code",
    "pid", "created_at", "updated_at",
)
REQUESTS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS requests ("
    "request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, state TEXT NOT NULL, "
    "response_ciphertext BLOB, error_code TEXT NOT NULL, pid INTEGER, "
    "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
)


def initialize_request_ledger_schema(connection: sqlite3.Connection) -> None:
    connection.execute(REQUESTS_TABLE_SQL)
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(requests)")
    }
    if "pid" not in columns:
        connection.execute("ALTER TABLE requests ADD COLUMN pid INTEGER")


__all__ = [
    "REQUEST_COLUMNS", "REQUESTS_TABLE_SQL", "initialize_request_ledger_schema",
]
