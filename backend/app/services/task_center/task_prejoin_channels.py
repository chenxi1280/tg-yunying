from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountGroupAdmissionFact, Action, Task, TgAccount, TgGroup
from app.services._common import _now, gateway


MAX_PREJOIN_CHANNELS = 3


class PrejoinOwnershipChanged(RuntimeError):
    """The completed prerequisite no longer belongs to this dispatch."""


@dataclass(frozen=True)
class PrejoinSnapshot:
    action_id: str
    tenant_id: int
    account_id: int
    group_id: int
    task_id: str
    claim: tuple
    task_state: tuple
    account_state: tuple
    session_ciphertext: str = field(repr=False)


def _claim_state(action: Action) -> tuple:
    return (action.status, action.claim_owner, action.claim_token,
            action.account_id, action.task_id, action.task_lifecycle_epoch)


def _task_state(task: Task) -> tuple:
    return (task.status, task.task_lifecycle_epoch, task.retired_at,
            task.deleted_at, tuple(_configured_refs(task)))


def _account_state(account: TgAccount) -> tuple:
    return (account.status, account.deleted_at, account.telegram_frozen)


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
    followed = _persisted_followed_refs(session, action, account, target_group=target_group)
    followed.update((action.result or {}).get("configured_channel_followed_refs") or [])
    pending = [ref for ref in refs if ref not in followed]
    if not pending:
        return True
    snapshot = PrejoinSnapshot(
        action_id=action.id, tenant_id=action.tenant_id, account_id=account.id,
        group_id=target_group.id, task_id=task.id, claim=_claim_state(action),
        task_state=_task_state(task), account_state=_account_state(account),
        session_ciphertext=account.session_ciphertext,
    )
    session.commit()
    results = _follow_parallel(snapshot, credentials, pending)
    _record_successes(session, snapshot, results)
    _refresh_ownership(session, snapshot)
    return _apply_results(action, followed, results)


def _apply_results(action: Action, followed: set[str], results: dict) -> bool:
    failures = {ref: result.detail for ref, result in results.items() if not result.ok}
    completed = (followed | {ref for ref, result in results.items() if result.ok}
                 | set((action.result or {}).get("configured_channel_followed_refs") or []))
    action.result = {
        **dict(action.result or {}),
        "configured_channel_followed_refs": sorted(completed),
    }
    if not failures:
        return True
    action.result = {
        **dict(action.result or {}),
        "error_code": "configured_channel_follow_failed",
        "configured_channel_follow_failures": failures,
    }
    return False


def _record_successes(session: Session, snapshot: PrejoinSnapshot, results: dict) -> None:
    for ref, result in results.items():
        if result.ok:
            _record_follow_fact(session, snapshot, channel_ref=ref, detail=result.detail)


def _refresh_ownership(session: Session, snapshot: PrejoinSnapshot) -> None:
    action = session.scalar(select(Action).where(Action.id == snapshot.action_id)
        .with_for_update().execution_options(populate_existing=True))
    task = session.get(Task, snapshot.task_id, populate_existing=True)
    account = session.get(TgAccount, snapshot.account_id, populate_existing=True)
    current = (
        action is not None and _claim_state(action) == snapshot.claim
        and task is not None and _task_state(task) == snapshot.task_state
        and account is not None and _account_state(account) == snapshot.account_state
        and account.session_ciphertext == snapshot.session_ciphertext
    )
    if current:
        return
    # A confirmed follow remains true even when this dispatch loses ownership.
    session.commit()
    raise PrejoinOwnershipChanged("configured_channel_follow_dispatch_changed")


def _persisted_followed_refs(
    session: Session,
    action: Action,
    account: TgAccount,
    *,
    target_group: TgGroup,
) -> set[str]:
    facts = session.scalars(
        select(AccountGroupAdmissionFact).where(
            AccountGroupAdmissionFact.tenant_id == action.tenant_id,
            AccountGroupAdmissionFact.account_id == account.id,
            AccountGroupAdmissionFact.target_group_id == target_group.id,
            AccountGroupAdmissionFact.fact_kind == "configured_channel_follow",
        )
    )
    return {
        str((fact.outcome or {}).get("channel_ref") or "").strip()
        for fact in facts
        if str((fact.outcome or {}).get("channel_ref") or "").strip()
    }


def _follow_parallel(snapshot: PrejoinSnapshot, credentials, refs: list[str]) -> dict:
    def follow(ref: str):
        return gateway.ensure_channel_membership(
            snapshot.account_id,
            ref,
            snapshot.session_ciphertext,
            credentials,
            invite_link=ref,
        )

    with ThreadPoolExecutor(max_workers=len(refs)) as executor:
        return dict(zip(refs, executor.map(follow, refs), strict=True))


def _record_follow_fact(
    session: Session,
    snapshot: PrejoinSnapshot,
    *,
    channel_ref: str,
    detail: str,
) -> None:
    identity = hashlib.sha256(
        f"{snapshot.account_id}:{snapshot.group_id}:{channel_ref}".encode()
    ).hexdigest()
    values = {
        "tenant_id": snapshot.tenant_id,
        "account_id": snapshot.account_id,
        "target_group_id": snapshot.group_id,
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
