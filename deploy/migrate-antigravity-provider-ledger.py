from __future__ import annotations

import hashlib
import json
import os
import pwd
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

SCRIPTS_DIR = Path(os.environ.get(
    "ANTIGRAVITY_PROVIDER_SCRIPTS_DIR",
    Path(__file__).resolve().parents[1] / "backend" / "scripts",
))
sys.path.insert(0, str(SCRIPTS_DIR))
from antigravity_provider_schema import (  # noqa: E402
    REQUEST_COLUMNS,
    initialize_request_ledger_schema,
)


OPEN_STATES = ("claimed", "started", "unknown")


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _snapshot(connection: sqlite3.Connection) -> tuple[int, str]:
    columns = [row[1] for row in connection.execute("PRAGMA table_info(requests)")]
    if not columns:
        raise RuntimeError("antigravity_legacy_ledger_schema_missing")
    rows = connection.execute("SELECT * FROM requests ORDER BY request_id").fetchall()
    serializable = [
        [value.hex() if isinstance(value, bytes) else value for value in row]
        for row in rows
    ]
    raw = json.dumps([columns, serializable], sort_keys=True, separators=(",", ":"))
    return len(rows), hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_settled(connection: sqlite3.Connection) -> None:
    placeholders = ",".join("?" for _state in OPEN_STATES)
    rows = connection.execute(
        f"SELECT request_id, state FROM requests WHERE state IN ({placeholders}) "
        "ORDER BY request_id",
        OPEN_STATES,
    ).fetchall()
    if rows:
        states = ",".join(f"{request_id}:{state}" for request_id, state in rows)
        raise RuntimeError(f"antigravity_legacy_ledger_reconcile_required:{states}")


def _backup(source_path: Path, temporary_path: Path) -> tuple[int, str]:
    with _open_read_only(source_path) as source:
        _require_settled(source)
        before = _snapshot(source)
        with sqlite3.connect(temporary_path) as destination:
            source.backup(destination)
    with _open_read_only(source_path) as source:
        _require_settled(source)
        after = _snapshot(source)
    with sqlite3.connect(temporary_path) as destination:
        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        copied = _snapshot(destination)
    if integrity != "ok" or before != after or copied != after:
        raise RuntimeError("antigravity_legacy_ledger_backup_drift")
    return copied


def _verified_snapshot(path: Path) -> tuple[int, str]:
    with _open_read_only(path) as connection:
        _require_settled(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        snapshot = _snapshot(connection)
    if integrity != "ok":
        raise RuntimeError("antigravity_ledger_integrity_failed")
    return snapshot


def _verified_staged_empty(path: Path) -> tuple[int, str]:
    with _open_read_only(path) as connection:
        _require_settled(connection)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        columns = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(requests)")
        )
        snapshot = _snapshot(connection)
    if integrity != "ok":
        raise RuntimeError("antigravity_ledger_integrity_failed")
    if columns != REQUEST_COLUMNS:
        raise RuntimeError("antigravity_staged_empty_schema_invalid")
    if snapshot[0] != 0:
        raise RuntimeError("antigravity_staged_empty_not_empty")
    return snapshot


def _temporary_path(destination_path: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=".ledger-migration-", suffix=".sqlite3", dir=destination_path.parent,
    )
    os.close(descriptor)
    return Path(name)


def _publish(path: Path, destination_path: Path, owner_name: str) -> None:
    owner = pwd.getpwnam(owner_name)
    os.chown(path, owner.pw_uid, owner.pw_gid)
    os.chmod(path, 0o600)
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    os.link(path, destination_path)
    directory_fd = os.open(destination_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def migrate(source_path: Path, destination_path: Path, owner_name: str) -> tuple[int, str]:
    if destination_path.exists():
        raise RuntimeError("antigravity_service_ledger_already_exists")
    temporary_path = _temporary_path(destination_path)
    try:
        snapshot = _backup(source_path, temporary_path)
        _publish(temporary_path, destination_path, owner_name)
        return snapshot
    finally:
        temporary_path.unlink(missing_ok=True)


def create_empty(destination_path: Path, owner_name: str) -> tuple[int, str]:
    if destination_path.exists():
        raise RuntimeError("antigravity_service_ledger_already_exists")
    temporary_path = _temporary_path(destination_path)
    try:
        with sqlite3.connect(temporary_path) as connection:
            initialize_request_ledger_schema(connection)
        snapshot = _verified_snapshot(temporary_path)
        if snapshot[0] != 0:
            raise RuntimeError("antigravity_empty_ledger_not_empty")
        _publish(temporary_path, destination_path, owner_name)
        return snapshot
    finally:
        temporary_path.unlink(missing_ok=True)


def resolve(
    configured_path: Path | None,
    legacy_path: Path,
    destination_path: Path,
    owner_name: str,
) -> tuple[str, tuple[int, str] | None]:
    if configured_path not in {None, legacy_path, destination_path}:
        raise RuntimeError("antigravity_ledger_authority_invalid")
    if configured_path == destination_path:
        if not destination_path.exists():
            raise RuntimeError("antigravity_service_ledger_missing")
        return "service_authoritative", _verified_snapshot(destination_path)
    if configured_path == legacy_path and not legacy_path.exists():
        if destination_path.exists():
            return "verified_staged_empty", _verified_staged_empty(destination_path)
        return "created_empty", create_empty(destination_path, owner_name)
    if legacy_path.exists():
        if not destination_path.exists():
            return "migrated", migrate(legacy_path, destination_path, owner_name)
        legacy = _verified_snapshot(legacy_path)
        destination = _verified_snapshot(destination_path)
        if legacy != destination:
            raise RuntimeError("antigravity_ledger_authority_drift")
        return "verified_equal", destination
    if destination_path.exists():
        return "verified_staged_empty", _verified_staged_empty(destination_path)
    return "created_empty", create_empty(destination_path, owner_name)


def main() -> int:
    if len(sys.argv) != 5:
        raise SystemExit(
            "usage: migrate-antigravity-provider-ledger.py CONFIGURED|- LEGACY DEST OWNER"
        )
    configured = None if sys.argv[1] == "-" else Path(sys.argv[1])
    action, snapshot = resolve(configured, Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    count, digest = snapshot or (0, "none")
    print(f"ANTIGRAVITY_LEDGER_ACTION={action} COUNT={count} SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
