from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

try:
    from scripts.antigravity_provider_schema import initialize_request_ledger_schema
except ModuleNotFoundError:  # Direct host execution from the immutable runtime.
    from antigravity_provider_schema import initialize_request_ledger_schema


@dataclass(frozen=True)
class LedgerRecord:
    request_id: str
    request_hash: str
    state: str
    response: dict | None
    error_code: str
    pid: int | None


class RequestLedger:
    def __init__(self, path: Path, key: str) -> None:
        if not key:
            raise RuntimeError("ANTIGRAVITY_LEDGER_KEY is required")
        self._path = path
        self._fernet = Fernet(key.encode("ascii"))
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def get(self, request_id: str) -> LedgerRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_id, request_hash, state, response_ciphertext, error_code, pid "
                "FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._record(row)

    def start(self, request_id: str, request_hash: str) -> LedgerRecord:
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT request_id, request_hash, state, response_ciphertext, error_code, pid "
                "FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                connection.commit()
                record = self._record(row)
                if record is None or record.request_hash != request_hash:
                    raise RuntimeError("antigravity_request_id_reused")
                return record
            connection.execute(
                "INSERT INTO requests "
                "(request_id, request_hash, state, response_ciphertext, error_code, pid, created_at, updated_at) "
                "VALUES (?, ?, 'claimed', NULL, '', NULL, ?, ?)",
                (request_id, request_hash, now, now),
            )
            connection.commit()
        return LedgerRecord(request_id, request_hash, "claimed", None, "", None)

    def reclaim(self, request_id: str, request_hash: str) -> LedgerRecord:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE requests SET state = 'claimed', error_code = '', updated_at = ? "
                "WHERE request_id = ? AND request_hash = ? AND state = 'not_started'",
                (time.time(), request_id, request_hash),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("antigravity_request_reclaim_failed")
        return LedgerRecord(request_id, request_hash, "claimed", None, "", None)

    def mark_started(self, request_id: str, pid: int) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE requests SET state = 'started', pid = ?, updated_at = ? "
                "WHERE request_id = ? AND state = 'claimed'",
                (pid, time.time(), request_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("antigravity_request_claim_lost")

    def settle(
        self,
        request_id: str,
        *,
        state: str,
        response: dict | None = None,
        error_code: str = "",
    ) -> None:
        ciphertext = self._encrypt(response) if response is not None else None
        with self._connect() as connection:
            connection.execute(
                "UPDATE requests SET state = ?, response_ciphertext = ?, "
                "error_code = ?, updated_at = ? WHERE request_id = ? "
                "AND state IN ('claimed', 'started')",
                (state, ciphertext, error_code, time.time(), request_id),
            )

    def _initialize(self) -> None:
        with self._connect() as connection:
            initialize_request_ledger_schema(connection)
        os.chmod(self._path, 0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _encrypt(self, payload: dict) -> bytes:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self._fernet.encrypt(raw)

    def _decrypt(self, value: bytes | None) -> dict | None:
        if value is None:
            return None
        try:
            payload = json.loads(self._fernet.decrypt(value).decode("utf-8"))
        except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError("antigravity_ledger_decrypt_failed") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("antigravity_invalid_envelope")
        return payload

    def _record(self, row) -> LedgerRecord | None:  # noqa: ANN001
        if row is None:
            return None
        return LedgerRecord(
            request_id=str(row[0]),
            request_hash=str(row[1]),
            state=str(row[2]),
            response=self._decrypt(row[3]),
            error_code=str(row[4] or ""),
            pid=int(row[5]) if row[5] is not None else None,
        )


__all__ = ["LedgerRecord", "RequestLedger"]
