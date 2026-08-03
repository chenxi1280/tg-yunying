from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import AccountGroupAdmissionFact, Action, Task, TgAccount, TgGroup
from app.services._common import _now, gateway


MAX_PREJOIN_CHANNELS = 3


def ensure_prejoin_channels(
    session: Session,
    *,
    task: Task,
    action: Action,
    account: TgAccount,
    credentials,
    target_group: TgGroup,
) -> bool:
    refs = _configured_refs(task)
    if not refs:
        return True
    followed = set((action.result or {}).get("configured_channel_followed_refs") or [])
    pending = [ref for ref in refs if ref not in followed]
    if not pending:
        return True
    results = _follow_parallel(account, credentials, pending)
    failures = {ref: result.detail for ref, result in results.items() if not result.ok}
    for ref, result in results.items():
        if result.ok:
            followed.add(ref)
            _record_follow_fact(
                session,
                action,
                account=account,
                target_group=target_group,
                channel_ref=ref,
                detail=result.detail,
            )
    action.result = {
        **dict(action.result or {}),
        "configured_channel_followed_refs": sorted(followed),
    }
    if not failures:
        return True
    action.result = {
        **dict(action.result or {}),
        "error_code": "configured_channel_follow_failed",
        "configured_channel_follow_failures": failures,
    }
    return False


def _follow_parallel(account: TgAccount, credentials, refs: list[str]) -> dict:
    def follow(ref: str):
        return gateway.ensure_channel_membership(
            account.id,
            ref,
            account.session_ciphertext,
            credentials,
            invite_link=ref,
        )

    with ThreadPoolExecutor(max_workers=len(refs)) as executor:
        return dict(zip(refs, executor.map(follow, refs), strict=True))


def _record_follow_fact(
    session: Session,
    action: Action,
    *,
    account: TgAccount,
    target_group: TgGroup,
    channel_ref: str,
    detail: str,
) -> None:
    identity = hashlib.sha256(
        f"{account.id}:{target_group.id}:{channel_ref}".encode()
    ).hexdigest()
    values = {
        "tenant_id": action.tenant_id,
        "account_id": account.id,
        "target_group_id": target_group.id,
        "fact_kind": "configured_channel_follow",
        "fact_identity_hash": identity,
        "fact_version": 1,
        "outcome": {"channel_ref": channel_ref, "detail": detail},
        "observed_at": _now(),
    }
    table = AccountGroupAdmissionFact.__table__
    insert = pg_insert(table) if session.get_bind().dialect.name == "postgresql" else sqlite_insert(table)
    session.execute(insert.values(**values).on_conflict_do_nothing(
        index_elements=["account_id", "target_group_id", "fact_kind", "fact_identity_hash"]
    ))


def _configured_refs(task: Task) -> list[str]:
    refs = list(task.group_ai_prejoin_channel_ids or [])
    if not refs:
        config = dict(task.type_config or {})
        refs = list(config.get("group_ai_prejoin_channel_ids") or [])
    normalized = [str(ref).strip() for ref in refs if str(ref).strip()]
    if len(normalized) > MAX_PREJOIN_CHANNELS:
        raise ValueError("group_ai_prejoin_channel_ids supports at most 3 values")
    return list(dict.fromkeys(normalized))


__all__ = ["ensure_prejoin_channels"]
