"""Remote-fact-first reconciliation for one AI-group rescue epoch."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter

from sqlalchemy import select
from telethon import errors, functions

from app.database import SessionLocal
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.integrations.telegram.telethon_utils import resolve_telethon_target
from app.models import AccountGroupAdmissionFact, Action, OperationTarget, Task, TgAccount
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_account
from app.services.task_center.ai_group_rescue_protected_recovery import (
    MembershipObservation,
    RecoveryScope,
    apply_admission_recovery,
    preview_admission_recovery,
)
from app.services.task_center.group_rescue import rescue_admin_account_id_for_task
from app.services.task_center.runtime_state_hash import canonical_state_hash


SOURCE_STATUSES = frozenset({"closed_unknown", "unknown_after_send", "skipped"})


class AdmissionProbeFloodWait(RuntimeError):
    def __init__(self, *, processed_count: int, retry_after_seconds: int) -> None:
        super().__init__("admission_recovery_probe_flood_wait")
        self.processed_count = processed_count
        self.retry_after_seconds = retry_after_seconds


async def main() -> int:
    args = _parser().parse_args()
    _validate_runtime_sha(args.deployed_sha)
    scope = _scope(args)
    try:
        with SessionLocal() as session:
            result = await _execute(session, scope, args)
    except AdmissionProbeFloodWait as exc:
        print(json.dumps(_probe_checkpoint(args, exc), sort_keys=True))
        raise
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


async def _execute(session, scope: RecoveryScope, args) -> dict:
    sources = _source_actions(session, scope)
    inventory = _inventory(sources)
    if args.mode == "inventory":
        return inventory
    _require_inventory(inventory, args)
    observations = await _observe_sources(session, scope, sources)
    if args.mode == "readback":
        return _readback(
            session,
            scope,
            observations=observations,
            inventory=inventory,
        )
    preview = preview_admission_recovery(session, scope, observations)
    if args.mode == "preview":
        return preview
    result = apply_admission_recovery(
        session,
        scope,
        observations,
        expected_fingerprint=args.expected_fingerprint,
        actor=args.actor,
        approval_reference=args.approval_ref,
    )
    session.commit()
    return result


def _source_actions(session, scope: RecoveryScope) -> tuple[Action, ...]:
    actions = session.scalars(select(Action).where(
        Action.task_id == scope.task_id,
        Action.action_type == "invite_group_account",
        Action.task_lifecycle_epoch == scope.expected_epoch,
        Action.status.in_(SOURCE_STATUSES),
    ).order_by(Action.id.asc()))
    matching = []
    for action in actions:
        if (action.result or {}).get("recovery_source") == "remote_absence":
            continue
        payload = dict(action.payload or {})
        identity = (
            int(payload.get("group_id") or 0),
            int(payload.get("operation_target_id") or 0),
        )
        if identity == (scope.expected_group_id, scope.expected_target_id):
            matching.append(action)
    if not matching:
        raise ValueError("admission_recovery_sources_missing")
    return tuple(matching)


def _inventory(sources: tuple[Action, ...]) -> dict:
    source_set = canonical_state_hash({
        "sources": [
            {
                "id": action.id,
                "status": action.status,
                "target_account_id": int((action.payload or {}).get("target_account_id") or 0),
            }
            for action in sources
        ],
    })
    status_counts = Counter(action.status for action in sources)
    return {
        "mode": "inventory",
        "source_count": len(sources),
        "source_set_fingerprint": source_set,
        "status_counts": dict(sorted(status_counts.items())),
    }


def _require_inventory(inventory: dict, args) -> None:
    observed = (
        inventory["source_count"],
        inventory["source_set_fingerprint"],
    )
    expected = (
        args.expected_source_count,
        args.expected_source_set_fingerprint,
    )
    if observed != expected:
        raise RuntimeError("admission_recovery_source_set_drift")


async def _observe_sources(
    session,
    scope: RecoveryScope,
    sources: tuple[Action, ...],
) -> tuple[MembershipObservation, ...]:
    client, target = await _admin_client_and_target(session, scope)
    observations = []
    for source in sources:
        try:
            observations.append(await _observe_source(client, target, source))
        except errors.FloodWaitError as exc:
            raise AdmissionProbeFloodWait(
                processed_count=len(observations),
                retry_after_seconds=int(exc.seconds),
            ) from exc
    return tuple(observations)


def _probe_checkpoint(args, exc: AdmissionProbeFloodWait) -> dict:
    return {
        "mode": "probe_checkpoint",
        "state": "stopped_flood_wait",
        "source_count": args.expected_source_count,
        "source_set_fingerprint": args.expected_source_set_fingerprint,
        "processed_count": exc.processed_count,
        "retry_after_seconds": exc.retry_after_seconds,
        "database_write_performed": False,
    }


async def _admin_client_and_target(session, scope: RecoveryScope):
    task = session.get(Task, scope.task_id)
    if task is None:
        raise ValueError("admission_recovery_task_missing")
    admin_id = rescue_admin_account_id_for_task(session, task)
    account = session.get(TgAccount, admin_id) if admin_id else None
    if account is None or account.deleted_at is not None:
        raise ValueError("admission_recovery_admin_missing")
    raw_session = decrypt_session(account.session_ciphertext)
    if not raw_session:
        raise ValueError("admission_recovery_admin_session_missing")
    gateway = TelethonTelegramGateway()
    credentials = credentials_for_account(session, account)
    client = await gateway._get_or_create_client(credentials, raw_session)
    if not await client.is_user_authorized():
        raise RuntimeError("admission_recovery_admin_unauthorized")
    target_row = session.get(OperationTarget, scope.expected_target_id)
    if target_row is None or not target_row.tg_peer_id:
        raise ValueError("admission_recovery_target_missing")
    target = await resolve_telethon_target(client, str(target_row.tg_peer_id), group_id=0)
    return client, target


async def _observe_source(client, target, source: Action) -> MembershipObservation:
    payload = dict(source.payload or {})
    account_id = int(payload.get("target_account_id") or 0)
    reference = str(payload.get("target_account_ref") or "").strip()
    if account_id <= 0 or not reference:
        raise RuntimeError("admission_recovery_source_reference_missing")
    try:
        entity = await client.get_entity(reference.lstrip("@"))
        result = await client(functions.channels.GetParticipantRequest(
            channel=target,
            participant=entity,
        ))
        outcome = "member"
        evidence_type = result.participant.__class__.__name__
    except errors.UserNotParticipantError as exc:
        outcome = "absent"
        evidence_type = exc.__class__.__name__
    except errors.FloodWaitError:
        raise
    except Exception as exc:
        outcome = "inconclusive"
        evidence_type = exc.__class__.__name__
    fingerprint = canonical_state_hash({
        "source_action_id": source.id,
        "target_account_id": account_id,
        "target_reference": reference,
        "remote_target_id": getattr(target, "id", 0),
        "outcome": outcome,
        "evidence_type": evidence_type,
    })
    return MembershipObservation(
        source_action_id=source.id,
        target_account_id=account_id,
        outcome=outcome,
        evidence_fingerprint=fingerprint,
    )


def _readback(
    session,
    scope: RecoveryScope,
    *,
    observations: tuple[MembershipObservation, ...],
    inventory: dict,
) -> dict:
    _assert_readback_task(session, scope)
    outcome_counts = Counter(item.outcome for item in observations)
    fact_count = _membership_fact_count(session, scope, observations)
    replacement_count = _replacement_count(session, scope, observations)
    return {
        "mode": "readback",
        "source_count": inventory["source_count"],
        "source_set_fingerprint": inventory["source_set_fingerprint"],
        "remote_outcome_counts": dict(sorted(outcome_counts.items())),
        "typed_membership_fact_count": fact_count,
        "replacement_action_count": replacement_count,
    }


def _assert_readback_task(session, scope: RecoveryScope) -> None:
    task = session.get(Task, scope.task_id)
    if task is None:
        raise ValueError("admission_readback_task_missing")
    config = dict(task.type_config or {})
    task_state = (
        int(task.task_lifecycle_epoch or 1),
        int(task.config_revision or 1),
        int(config.get("target_operation_target_id") or 0),
        int(config.get("target_group_id") or 0),
    )
    expected_state = (
        scope.expected_epoch,
        scope.expected_config_revision,
        scope.expected_target_id,
        scope.expected_group_id,
    )
    if task_state != expected_state:
        raise RuntimeError("admission_readback_task_state_drift")


def _membership_fact_count(
    session,
    scope: RecoveryScope,
    observations: tuple[MembershipObservation, ...],
) -> int:
    fact_hashes = [
        canonical_state_hash({
            "source_action_id": item.source_action_id,
            "target_account_id": item.target_account_id,
            "evidence_fingerprint": item.evidence_fingerprint,
        })
        for item in observations
        if item.outcome == "member"
    ]
    if not fact_hashes:
        return 0
    facts = session.scalars(select(AccountGroupAdmissionFact).where(
        AccountGroupAdmissionFact.target_group_id == scope.expected_group_id,
        AccountGroupAdmissionFact.fact_kind == "membership_observed",
        AccountGroupAdmissionFact.fact_identity_hash.in_(fact_hashes),
    ))
    fact_count = sum(
        1
        for fact in facts
        if (fact.outcome or {}).get("source") == "group_rescue_read_only_reconcile"
    )
    return fact_count


def _replacement_count(
    session,
    scope: RecoveryScope,
    observations: tuple[MembershipObservation, ...],
) -> int:
    source_ids = {item.source_action_id for item in observations}
    replacements = session.scalars(select(Action).where(
        Action.task_id == scope.task_id,
        Action.task_lifecycle_epoch == scope.expected_epoch,
        Action.action_type == "invite_group_account",
    ))
    replacement_count = sum(
        1
        for action in replacements
        if (action.result or {}).get("recovery_source") == "remote_absence"
        and (action.result or {}).get("recovery_source_action_id") in source_ids
    )
    return replacement_count


def _scope(args) -> RecoveryScope:
    return RecoveryScope(
        task_id=args.task_id,
        expected_epoch=args.expected_epoch,
        expected_config_revision=args.expected_config_revision,
        expected_target_id=args.expected_target_id,
        expected_group_id=args.expected_group_id,
        deployed_sha=args.deployed_sha,
    )


def _validate_runtime_sha(deployed_sha: str) -> None:
    runtime_sha = str(os.getenv("RELEASE_SHA") or os.getenv("GIT_SHA") or "").lower()
    if len(deployed_sha) != 40 or runtime_sha != deployed_sha.lower():
        raise RuntimeError("admission_recovery_deployed_sha_mismatch")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Reconcile one AI-group rescue epoch")
    parser.add_argument("--mode", choices=("inventory", "preview", "apply", "readback"), required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-epoch", type=int, required=True)
    parser.add_argument("--expected-config-revision", type=int, required=True)
    parser.add_argument("--expected-target-id", type=int, required=True)
    parser.add_argument("--expected-group-id", type=int, required=True)
    parser.add_argument("--expected-source-count", type=int, default=0)
    parser.add_argument("--expected-source-set-fingerprint", default="")
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
