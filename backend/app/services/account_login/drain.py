from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from app.config import get_settings
from app.models import TgAccountLoginBatch, TgAccountLoginBatchItem
from app.services._common import _now
from app.services.code_source_client import CodeSourceClient

from .local_phases import execute_local_phase
from .remote_phases import execute_remote_phase
from .state import EXTERNAL_PHASES, claim_batch_phase


ACTIVE_BATCH_STATUSES = ("queued", "running", "cancelling")


def drain_account_login_batches(session_factory, limit: int, *, code_client: CodeSourceClient | None = None) -> int:
    if get_settings().account_batch_login_mode != "enabled":
        return 0
    client = code_client or CodeSourceClient()
    processed = 0
    for batch_id in _fair_batch_ids(session_factory, limit):
        with session_factory() as session:
            claim = claim_batch_phase(session, batch_id)
        if not claim:
            continue
        if claim.phase in EXTERNAL_PHASES:
            execute_remote_phase(session_factory, claim, client)
        else:
            with session_factory() as session:
                execute_local_phase(session, claim)
        processed += 1
    _clear_expired_credentials(session_factory)
    return processed


def _fair_batch_ids(session_factory, limit: int) -> list[int]:
    with session_factory() as session:
        batches = list(session.scalars(select(TgAccountLoginBatch).where(
            TgAccountLoginBatch.status.in_(ACTIVE_BATCH_STATUSES),
        ).order_by(
            TgAccountLoginBatch.last_claimed_at.asc().nullsfirst(),
            TgAccountLoginBatch.created_at.asc(),
            TgAccountLoginBatch.id.asc(),
        ).limit(max(1, limit) * 8)))
    by_tenant: dict[int, list[TgAccountLoginBatch]] = defaultdict(list)
    for batch in batches:
        by_tenant[batch.tenant_id].append(batch)
    selected: list[int] = []
    while len(selected) < max(1, limit) and by_tenant:
        for tenant_id in sorted(by_tenant, key=lambda value: _tenant_sort_key(by_tenant[value])):
            rows = by_tenant[tenant_id]
            selected.append(rows.pop(0).id)
            if not rows:
                by_tenant.pop(tenant_id)
            if len(selected) >= max(1, limit):
                break
    return selected


def _tenant_sort_key(batches: list[TgAccountLoginBatch]):
    first = batches[0]
    return (first.last_claimed_at or first.created_at, first.tenant_id)


def _clear_expired_credentials(session_factory) -> None:
    with session_factory() as session:
        now = _now()
        items = session.scalars(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.code_url_ciphertext.is_not(None),
            TgAccountLoginBatchItem.credential_expires_at.is_not(None),
            TgAccountLoginBatchItem.credential_expires_at <= now,
        ).limit(200).with_for_update(skip_locked=True))
        changed = False
        for item in items:
            item.code_url_ciphertext = None
            item.state_version += 1
            changed = True
        if changed:
            session.commit()


__all__ = ["drain_account_login_batches"]
