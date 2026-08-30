"""Guarded live binding repair for one AI-group task."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import asdict, dataclass

from telethon import functions

from app.database import SessionLocal
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.integrations.telegram.telethon_utils import resolve_telethon_target
from app.models import Task, TgAccount
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_account
from app.services.task_center.ai_group_rescue_protected_recovery import (
    BindingEvidence,
    RecoveryScope,
    apply_binding_recovery,
    preview_binding_recovery,
)
from app.services.task_center.runtime_state_hash import canonical_state_hash


@dataclass(frozen=True)
class RemoteRoleProbe:
    identity_digest: str
    target_peer_digest: str
    is_member: bool
    is_admin: bool
    invite_users: bool
    ban_users: bool
    delete_messages: bool
    history_readable: bool


async def main() -> int:
    args = _parser().parse_args()
    _validate_runtime_sha(args.deployed_sha)
    scope = _scope(args)
    with SessionLocal() as session:
        result = await _execute(session, scope, args)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


async def _execute(session, scope: RecoveryScope, args) -> dict:
    if args.mode == "readback":
        return await _readback(session, scope, args)
    evidence = await _live_evidence(
        session,
        scope,
        admin_id=args.rescue_admin_account_id,
        listener_id=args.listener_account_id,
    )
    if args.mode == "preview":
        return preview_binding_recovery(session, scope, evidence)
    result = apply_binding_recovery(
        session,
        scope,
        evidence,
        expected_fingerprint=args.expected_fingerprint,
        actor=args.actor,
        approval_reference=args.approval_ref,
    )
    session.commit()
    return result


async def _readback(session, scope: RecoveryScope, args) -> dict:
    task = session.get(Task, scope.task_id)
    if task is None:
        raise ValueError("binding_readback_task_missing")
    config = dict(task.type_config or {})
    observed = (
        int(config.get("target_operation_target_id") or 0),
        int(config.get("target_group_id") or 0),
        int(config.get("group_rescue_admin_account_id") or 0),
        int(config.get("history_fetch_account_id") or 0),
    )
    expected = (
        scope.expected_target_id,
        scope.expected_group_id,
        args.rescue_admin_account_id,
        args.listener_account_id,
    )
    if observed != expected:
        raise RuntimeError("binding_readback_state_mismatch")
    evidence = await _live_evidence(
        session,
        scope,
        admin_id=args.rescue_admin_account_id,
        listener_id=args.listener_account_id,
    )
    return {
        "mode": "readback",
        "config_revision": int(task.config_revision or 1),
        "task_scope_hash": canonical_state_hash({"tenant": task.tenant_id, "task": task.id}),
        "remote_evidence": _sanitized_evidence(evidence),
    }


async def _live_evidence(
    session,
    scope: RecoveryScope,
    *,
    admin_id: int,
    listener_id: int,
) -> BindingEvidence:
    admin = await _probe_account(session, scope, admin_id, require_history=False)
    listener = await _probe_account(session, scope, listener_id, require_history=True)
    if admin.target_peer_digest != listener.target_peer_digest:
        raise RuntimeError("binding_remote_target_mismatch")
    return BindingEvidence(
        rescue_admin_account_id=admin_id,
        listener_account_id=listener_id,
        target_peer_digest=admin.target_peer_digest,
        rescue_identity_digest=admin.identity_digest,
        listener_identity_digest=listener.identity_digest,
        rescue_is_admin=admin.is_admin,
        invite_users=admin.invite_users,
        ban_users=admin.ban_users,
        delete_messages=admin.delete_messages,
        listener_is_member=listener.is_member,
        listener_history_readable=listener.history_readable,
    )


async def _probe_account(
    session,
    scope: RecoveryScope,
    account_id: int,
    *,
    require_history: bool,
) -> RemoteRoleProbe:
    account = session.get(TgAccount, account_id)
    if account is None or account.deleted_at is not None:
        raise ValueError("binding_probe_account_missing")
    raw_session = decrypt_session(account.session_ciphertext)
    if not raw_session:
        raise ValueError("binding_probe_session_missing")
    credentials = credentials_for_account(session, account)
    gateway = TelethonTelegramGateway()
    client = await gateway._get_or_create_client(credentials, raw_session)
    if not await client.is_user_authorized():
        raise RuntimeError("binding_probe_session_unauthorized")
    target = await resolve_telethon_target(client, _target_peer(session, scope), group_id=0)
    me = await client.get_me()
    participant = await client(functions.channels.GetParticipantRequest(
        channel=target,
        participant=me,
    ))
    history_readable = await _history_readable(client, target) if require_history else False
    rights = _participant_rights(participant.participant)
    return RemoteRoleProbe(
        identity_digest=canonical_state_hash({"id": me.id, "username": me.username or ""}),
        target_peer_digest=canonical_state_hash({"id": target.id, "peer": _target_peer(session, scope)}),
        is_member=True,
        is_admin=rights[0],
        invite_users=rights[1],
        ban_users=rights[2],
        delete_messages=rights[3],
        history_readable=history_readable,
    )


async def _history_readable(client, target) -> bool:
    await client(functions.messages.GetHistoryRequest(
        peer=target,
        offset_id=0,
        offset_date=None,
        add_offset=0,
        limit=1,
        max_id=0,
        min_id=0,
        hash=0,
    ))
    return True


def _participant_rights(participant) -> tuple[bool, bool, bool, bool]:
    class_name = participant.__class__.__name__
    creator = class_name == "ChannelParticipantCreator"
    rights = getattr(participant, "admin_rights", None)
    is_admin = creator or class_name == "ChannelParticipantAdmin"
    return (
        is_admin,
        creator or bool(getattr(rights, "invite_users", False)),
        creator or bool(getattr(rights, "ban_users", False)),
        creator or bool(getattr(rights, "delete_messages", False)),
    )


def _target_peer(session, scope: RecoveryScope) -> str:
    from app.models import OperationTarget

    target = session.get(OperationTarget, scope.expected_target_id)
    if target is None or not target.tg_peer_id:
        raise ValueError("binding_probe_target_missing")
    return str(target.tg_peer_id)


def _sanitized_evidence(evidence: BindingEvidence) -> dict:
    body = asdict(evidence)
    body.pop("rescue_admin_account_id")
    body.pop("listener_account_id")
    return body


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
        raise RuntimeError("binding_recovery_deployed_sha_mismatch")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Repair one AI-group rescue binding")
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), required=True)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--expected-epoch", type=int, required=True)
    parser.add_argument("--expected-config-revision", type=int, required=True)
    parser.add_argument("--expected-target-id", type=int, required=True)
    parser.add_argument("--expected-group-id", type=int, required=True)
    parser.add_argument("--rescue-admin-account-id", type=int, required=True)
    parser.add_argument("--listener-account-id", type=int, required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    return parser


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
