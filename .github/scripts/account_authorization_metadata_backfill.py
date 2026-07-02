from __future__ import annotations

import json
import os

from app.database import SessionLocal
from app.services.account_authorization_backfill import backfill_standby_authorization_metadata


ACTOR = "account-authorization-metadata-backfill"


def main() -> None:
    tenant_id = _int_env("ACCOUNT_AUTH_BACKFILL_TENANT_ID", 1)
    limit = _int_env("ACCOUNT_AUTH_BACKFILL_LIMIT", 1000)
    account_id = _optional_int_env("ACCOUNT_AUTH_BACKFILL_ACCOUNT_ID")
    apply = _bool_env("ACCOUNT_AUTH_BACKFILL_APPLY")
    with SessionLocal() as session:
        result = backfill_standby_authorization_metadata(
            session,
            tenant_id=tenant_id,
            apply=apply,
            actor=ACTOR,
            limit=limit,
            account_id=account_id,
        )
    print("ACCOUNT_AUTHORIZATION_METADATA_BACKFILL=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _optional_int_env(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return int(value) if value else None


if __name__ == "__main__":
    main()
