from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.integrations.telegram.update_contracts import TelegramDifferenceBatch
from app.models import (
    AccountStatus,
    Action,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    Task,
    TgAccount,
    TgAccountAuthorization,
)
from app.models.group_clone import CloneSourceStreamState, TelegramGatewayMutationIdentity
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.services._common import _now, audit, gateway
from app.services.developer_apps import credentials_for_authorization

from .telegram_update_reconcile import reconcile_update_mappings
from .telegram_update_ingress import (
    NormalizedUpdateIngress,
    ingest_normalized_update,
    record_outbound_random_id_mapping,
)

COLLECTOR_LEASE_SECONDS = 90
COLLECTOR_STATES = ("initializing", "catching_up", "live", "gap")
SOURCE_STREAM_STATES = ("catching_up", "live", "gap")


@dataclass(frozen=True, kw_only=True)
class CollectorClaim:
    state_id: str
    owner_id: str
    fencing_epoch: int
    authorization_id: int
    session_generation: int
    initialize: bool
    cursor: dict[str, int]


@dataclass
class CollectorDrainResult:
    source_count: int = 0
    batch_count: int = 0
    event_count: int = 0
    mapping_count: int = 0
    reconciled_count: int = 0
    error_count: int = 0

    @property
    def processed_count(self) -> int:
        return self.batch_count + self.reconciled_count + self.error_count


def drain_telegram_update_collector(
    session_factory,
    *,
    tenant_id: int | None = None,
    limit: int = 50,
) -> CollectorDrainResult:
    state_ids = _candidate_state_ids(session_factory, tenant_id=tenant_id, limit=limit)
    result = CollectorDrainResult(source_count=len(state_ids))
    for state_id in state_ids:
        claim = _claim_state(session_factory, state_id)
        if claim is None:
            continue
        try:
            mapping_ids = _drain_claim(session_factory, claim, result)
            result.reconciled_count += reconcile_update_mappings(session_factory, mapping_ids)
        except Exception as exc:
            _record_claim_error(session_factory, claim, exc)
            result.error_count += 1
    return result


def _candidate_state_ids(session_factory, *, tenant_id, limit) -> list[str]:
    conditions = [TelegramAuthorizationUpdateState.state.in_(COLLECTOR_STATES)]
    if tenant_id is not None:
        conditions.append(TelegramAuthorizationUpdateState.tenant_id == tenant_id)
    with session_factory() as session:
        return list(session.scalars(
            select(TelegramAuthorizationUpdateState.id)
            .where(*conditions)
            .order_by(TelegramAuthorizationUpdateState.updated_at, TelegramAuthorizationUpdateState.id)
            .limit(max(1, int(limit or 0)))
        ))


def _claim_state(session_factory, state_id: str) -> CollectorClaim | None:
    owner_id = _collector_owner()
    with session_factory() as session:
        state = session.scalar(select(TelegramAuthorizationUpdateState).where(
            TelegramAuthorizationUpdateState.id == state_id,
        ).with_for_update())
        if state is None or state.state not in COLLECTOR_STATES:
            return None
        if _leased_by_other(state, owner_id):
            return None
        initialize = not int(state.common_date or 0) or not int(state.common_pts or 0)
        previous_owner = str(state.owner_id or "")
        if previous_owner and previous_owner != owner_id:
            state.owner_fencing_epoch = int(state.owner_fencing_epoch or 1) + 1
        state.owner_id = owner_id
        state.lease_expires_at = _now() + timedelta(seconds=COLLECTOR_LEASE_SECONDS)
        state.state = "catching_up" if initialize or state.state == "gap" else state.state
        state.version = int(state.version or 1) + 1
        if previous_owner != owner_id:
            _audit_owner_change(session, state, previous_owner=previous_owner)
        claim = CollectorClaim(
            state_id=state.id,
            owner_id=owner_id,
            fencing_epoch=int(state.owner_fencing_epoch or 1),
            authorization_id=state.authorization_id,
            session_generation=state.session_generation,
            initialize=initialize,
            cursor=_common_cursor(state),
        )
        session.commit()
        return claim


def _drain_claim(session_factory, claim: CollectorClaim, result: CollectorDrainResult) -> list[str]:
    runtime = _authorization_runtime(session_factory, claim)
    batch = _fetch_common_batch(claim, runtime)
    mapping_ids = _persist_batch(session_factory, claim, batch)
    _count_batch(result, batch, mapping_ids)
    if batch.status == "too_long":
        return mapping_ids
    for peer_id, pts in _channel_cursors(session_factory, claim.state_id):
        channel_batch = gateway.fetch_raw_channel_difference(
            peer_id,
            pts,
            session_ciphertext=runtime[0],
            credentials=runtime[1],
        )
        mapping_ids.extend(_persist_batch(session_factory, claim, channel_batch, peer_id=peer_id))
        _count_batch(result, channel_batch, mapping_ids=[])
    return mapping_ids


def _authorization_runtime(session_factory, claim: CollectorClaim):
    with session_factory() as session:
        authorization = session.get(TgAccountAuthorization, claim.authorization_id)
        account = session.get(TgAccount, authorization.account_id) if authorization else None
        if authorization is None or account is None:
            raise RuntimeError("telegram_update_collector_authorization_missing")
        if account.status != AccountStatus.ACTIVE.value or account.deleted_at is not None:
            raise RuntimeError("telegram_update_collector_account_not_online")
        if not authorization.is_current or authorization.status != "active":
            raise RuntimeError("telegram_update_collector_authorization_not_current")
        if int(authorization.slot_generation or 1) != claim.session_generation:
            raise RuntimeError("telegram_update_collector_session_generation_mismatch")
        credentials = credentials_for_authorization(session, authorization)
        if credentials is None:
            raise RuntimeError("telegram_update_collector_credentials_missing")
        return authorization.session_ciphertext, credentials


def _fetch_common_batch(claim: CollectorClaim, runtime) -> TelegramDifferenceBatch:
    if claim.initialize:
        return gateway.fetch_raw_authorization_update_state(
            session_ciphertext=runtime[0],
            credentials=runtime[1],
        )
    return gateway.fetch_raw_authorization_difference(
        claim.cursor,
        session_ciphertext=runtime[0],
        credentials=runtime[1],
    )


def _persist_batch(
    session_factory,
    claim: CollectorClaim,
    batch: TelegramDifferenceBatch,
    *,
    peer_id: str | None = None,
) -> list[str]:
    with session_factory() as session:
        state = _owned_state(session, claim)
        event_hashes = _persist_updates(session, state, claim=claim, batch=batch)
        mapping_ids = _persist_mappings(
            session,
            state,
            claim=claim,
            batch=batch,
            event_hashes=event_hashes,
        )
        if batch.scope == "common":
            _apply_common_batch(state, batch)
        else:
            _apply_channel_batch(session, state, batch, peer_id=peer_id)
        state.lease_expires_at = _now() + timedelta(seconds=COLLECTOR_LEASE_SECONDS)
        state.version = int(state.version or 1) + 1
        session.commit()
        return mapping_ids


def _persist_updates(session, state, *, claim, batch) -> dict[str, str]:
    event_hashes: dict[str, str] = {}
    for update in batch.updates:
        event, _ = ingest_normalized_update(
            session,
            state.id,
            NormalizedUpdateIngress(
                update_identity_key=update.identity_key,
                constructor_name=update.constructor_name,
                pts_evidence=update.pts,
                pts_count_evidence=update.pts_count,
                routing_peer_type=update.routing_peer_type,
                routing_peer_id=update.routing_peer_id,
                normalized_items=update.normalized_items,
                cursor_scope="event_only",
            ),
            owner_id=claim.owner_id,
            owner_fencing_epoch=claim.fencing_epoch,
        )
        event_hashes[update.identity_key] = event.update_identity_hash
    return event_hashes


def _persist_mappings(session, state, *, claim, batch, event_hashes) -> list[str]:
    mapping_ids: list[str] = []
    for item in batch.outbound_mappings:
        identity = _mutation_identity(session, claim, item.random_id)
        if identity is None:
            continue
        action, attempt, journal = _mapping_runtime(session, identity)
        mapping = record_outbound_random_id_mapping(
            session,
            state.id,
            random_id=item.random_id,
            target_peer_type=identity.target_peer_type,
            target_peer_id=identity.target_peer_id,
            remote_message_or_topic_id=str(item.remote_message_id),
            action_id=action.id if action else None,
            execution_attempt_id=attempt.id if attempt else None,
            gateway_mutation_identity_id=identity.id,
            gateway_request_journal_id=journal.id if journal else None,
            update_identity_hash=event_hashes.get(item.update_identity_key),
        )
        if action and attempt and action.status == "unknown_after_send":
            mapping_ids.append(mapping.id)
    return mapping_ids


def _mutation_identity(session, claim, random_id):
    return session.scalar(select(TelegramGatewayMutationIdentity).where(
        TelegramGatewayMutationIdentity.authorization_id == claim.authorization_id,
        TelegramGatewayMutationIdentity.session_generation == claim.session_generation,
        TelegramGatewayMutationIdentity.random_id == random_id,
    ).with_for_update())


def _mapping_runtime(session, identity):
    actions = session.scalars(select(Action).where(
        Action.task_id == identity.task_id,
        Action.task_lifecycle_epoch == identity.epoch,
        Action.obligation_id == identity.obligation_id,
        Action.action_type.in_(("group_clone_send", "group_clone_mutation")),
    ).order_by(Action.created_at.desc()).with_for_update()).all()
    action = next(
        (
            item for item in actions
            if str((item.payload or {}).get("gateway_mutation_identity_id") or "")
            == identity.id
        ),
        None,
    )
    attempt = session.scalar(select(ExecutionAttempt).where(
        ExecutionAttempt.action_id == action.id,
    ).order_by(ExecutionAttempt.attempt_no.desc()).limit(1).with_for_update()) if action else None
    journal = session.scalar(select(GatewayRequestEvidenceJournal).where(
        GatewayRequestEvidenceJournal.execution_attempt_id == attempt.id,
    ).limit(1)) if attempt else None
    return action, attempt, journal


def _apply_common_batch(state, batch) -> None:
    cursor = dict(batch.cursor or {})
    for key, attr in (("pts", "common_pts"), ("qts", "common_qts"), ("seq", "common_seq"), ("date", "common_date")):
        if key in cursor:
            setattr(state, attr, int(cursor[key] or 0))
    current = dict(state.difference_cursor or {})
    current["common"] = {**_common_cursor(state), "status": batch.status}
    current.pop("last_error", None)
    current.pop("last_error_at", None)
    state.difference_cursor = current
    state.state = "blocked" if batch.status == "too_long" else "catching_up" if not batch.final else "live"
    state.last_applied_at = _now()


def _apply_channel_batch(session, state, batch, *, peer_id) -> None:
    if not peer_id:
        raise RuntimeError("telegram_channel_difference_peer_missing")
    current = dict(state.difference_cursor or {})
    channels = dict(current.get("channels") or {})
    channels[peer_id] = {
        **dict(batch.cursor or {}),
        "status": batch.status,
        "final": bool(batch.final),
    }
    current["channels"] = channels
    state.difference_cursor = current
    state.last_applied_at = _now()
    if batch.status == "too_long" or not batch.final:
        reason = (
            "group_clone_channel_difference_too_long"
            if batch.status == "too_long"
            else "group_clone_channel_difference_incomplete"
        )
        _block_channel_streams(session, state.id, peer_id, reason=reason)
        return
    _recover_channel_streams(session, state.id, peer_id)


def _channel_cursors(session_factory, state_id: str) -> list[tuple[str, int]]:
    with session_factory() as session:
        state = session.get(TelegramAuthorizationUpdateState, state_id)
        channel_state = dict((state.difference_cursor or {}).get("channels") or {})
        rows = session.execute(
            select(
                CloneSourceStreamState.source_peer_id,
                func.min(CloneSourceStreamState.channel_pts),
            )
            .join(Task, Task.id == CloneSourceStreamState.task_id)
            .where(
                CloneSourceStreamState.authorization_update_state_id == state_id,
                CloneSourceStreamState.state.in_(SOURCE_STREAM_STATES),
                CloneSourceStreamState.channel_pts > 0,
                Task.status.in_(("pending", "running", "failed")),
                Task.task_lifecycle_epoch == CloneSourceStreamState.task_lifecycle_epoch,
            )
            .group_by(CloneSourceStreamState.source_peer_id)
        ).all()
        return [
            (
                str(peer_id),
                max(int(pts), int((channel_state.get(str(peer_id)) or {}).get("pts") or 0)),
            )
            for peer_id, pts in rows
        ]


def _block_channel_streams(session, state_id: str, peer_id: str, *, reason: str) -> None:
    streams = list(session.scalars(select(CloneSourceStreamState).where(
        CloneSourceStreamState.authorization_update_state_id == state_id,
        CloneSourceStreamState.source_peer_id == peer_id,
        CloneSourceStreamState.state.in_(SOURCE_STREAM_STATES),
    ).with_for_update()))
    for stream in streams:
        stream.state = "gap"
        task = session.get(Task, stream.task_id)
        if task and task.task_lifecycle_epoch == stream.task_lifecycle_epoch:
            task.status = "failed"
            task.last_error = reason
            task.stats = {**dict(task.stats or {}), "clone_start_state": "runtime_blocked"}


def _recover_channel_streams(session, state_id: str, peer_id: str) -> None:
    streams = list(session.scalars(select(CloneSourceStreamState).where(
        CloneSourceStreamState.authorization_update_state_id == state_id,
        CloneSourceStreamState.source_peer_id == peer_id,
        CloneSourceStreamState.state == "gap",
    ).with_for_update()))
    for stream in streams:
        task = session.get(Task, stream.task_id)
        if task is None or task.task_lifecycle_epoch != stream.task_lifecycle_epoch:
            continue
        stream.state = "catching_up"
        stream.version = int(stream.version or 1) + 1
        task.status = "running"
        task.last_error = ""
        task.stats = {
            **dict(task.stats or {}),
            "clone_start_state": "runtime_recovering",
        }


def _owned_state(session: Session, claim: CollectorClaim):
    state = session.scalar(select(TelegramAuthorizationUpdateState).where(
        TelegramAuthorizationUpdateState.id == claim.state_id,
    ).with_for_update())
    if state is None:
        raise RuntimeError("telegram_update_collector_state_missing")
    if state.owner_id != claim.owner_id or state.owner_fencing_epoch != claim.fencing_epoch:
        raise RuntimeError("telegram_update_collector_fenced")
    if state.lease_expires_at is None or _naive(state.lease_expires_at) <= _naive(_now()):
        raise RuntimeError("telegram_update_collector_lease_expired")
    return state


def _record_claim_error(session_factory, claim: CollectorClaim, exc: Exception) -> None:
    with session_factory() as session:
        state = session.scalar(select(TelegramAuthorizationUpdateState).where(
            TelegramAuthorizationUpdateState.id == claim.state_id,
        ).with_for_update())
        if state is None or state.owner_id != claim.owner_id or state.owner_fencing_epoch != claim.fencing_epoch:
            return
        cursor = dict(state.difference_cursor or {})
        cursor["last_error"] = str(exc).strip() or type(exc).__name__
        cursor["last_error_at"] = _now().isoformat()
        state.difference_cursor = cursor
        state.state = "gap"
        state.lease_expires_at = _now()
        state.version = int(state.version or 1) + 1
        session.commit()


def _leased_by_other(state, owner_id: str) -> bool:
    return bool(
        state.owner_id
        and state.owner_id != owner_id
        and state.lease_expires_at
        and _naive(state.lease_expires_at) > _naive(_now())
    )


def _common_cursor(state) -> dict[str, int]:
    return {
        "pts": int(state.common_pts or 0),
        "qts": int(state.common_qts or 0),
        "seq": int(state.common_seq or 0),
        "date": int(state.common_date or 0),
    }


def _audit_owner_change(session, state, *, previous_owner: str) -> None:
    audit(
        session,
        tenant_id=state.tenant_id,
        actor="telegram-update-collector",
        action="update_ingress_owner_changed",
        target_type="telegram_authorization_update_state",
        target_id=state.id,
        detail=f"previous_owner_present={bool(previous_owner)}; fencing_epoch={state.owner_fencing_epoch}",
    )


def _count_batch(result, batch, mapping_ids) -> None:
    result.batch_count += 1
    result.event_count += len(batch.updates)
    result.mapping_count += len(mapping_ids)


def _collector_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:telegram-update"[:64]


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = ["CollectorDrainResult", "drain_telegram_update_collector"]
