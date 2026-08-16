from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, wait

from sqlalchemy import select

from app.config import get_settings
from app.models import TgAccountLoginBatch, TgAccountLoginBatchItem
from app.services._common import _now
from app.services.code_source_client import CodeSourceClient

from .local_phases import execute_local_phase
from .remote_phases import execute_remote_phase
from .state import EXTERNAL_PHASES, LEASE_SECONDS, PhaseClaim, claim_batch_phase


ACTIVE_BATCH_STATUSES = ("queued", "running", "cancelling")
PHASE_JOIN_GRACE_SECONDS = 5
PHASE_JOIN_TIMEOUT_SECONDS = LEASE_SECONDS + PHASE_JOIN_GRACE_SECONDS
logger = logging.getLogger(__name__)


def drain_account_login_batches(session_factory, limit: int, *, code_client: CodeSourceClient | None = None) -> int:
    settings = get_settings()
    if settings.account_batch_login_mode != "enabled":
        return 0
    client = code_client or CodeSourceClient()
    slot_count = min(max(1, limit), settings.account_batch_login_worker_concurrency)
    claims = _claim_fair_phases(session_factory, slot_count)
    _execute_claims(session_factory, claims, client)
    _clear_expired_credentials(session_factory)
    return len(claims)


def _claim_fair_phases(session_factory, limit: int) -> list[PhaseClaim]:
    claims: list[PhaseClaim] = []
    while len(claims) < limit:
        candidate_limit = max(1, limit - len(claims)) * 8
        claimed_this_round = 0
        for batch_id in _fair_batch_ids(session_factory, candidate_limit):
            if len(claims) >= limit:
                break
            with session_factory() as session:
                claim = claim_batch_phase(session, batch_id)
            if claim:
                claims.append(claim)
                claimed_this_round += 1
        if not claimed_this_round:
            break
    return claims


def _execute_claims(session_factory, claims: list[PhaseClaim], client: CodeSourceClient) -> None:
    if not claims:
        return
    if len(claims) == 1:
        _execute_claim(session_factory, claims[0], client)
        return
    executor = ThreadPoolExecutor(max_workers=len(claims), thread_name_prefix="account-login")
    try:
        futures = [executor.submit(_execute_claim, session_factory, claim, client) for claim in claims]
        done, pending = wait(futures, timeout=PHASE_JOIN_TIMEOUT_SECONDS)
        for future in done:
            future.result()
        if pending:
            logger.warning("account login remote phases exceeded lease window pending=%d", len(pending))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _execute_claim(session_factory, claim: PhaseClaim, client: CodeSourceClient) -> None:
    if claim.phase in EXTERNAL_PHASES:
        execute_remote_phase(session_factory, claim, client)
        return
    with session_factory() as session:
        execute_local_phase(session, claim)


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
