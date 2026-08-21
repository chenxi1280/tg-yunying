from __future__ import annotations

import argparse
import hashlib
import json

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, TgAccount, TgAuthorizationDrBatchItem, TgAuthorizationDrOperation
from app.services._common import audit
from app.services.authorization_dr import AuthorizationDrError, project_authoritative_login_failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project persisted Telegram phone-ban facts onto account truth")
    parser.add_argument("--mode", choices=("preview", "apply", "readback"), required=True)
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--requested-by", required=True)
    parser.add_argument("--actor")
    parser.add_argument("--approval-ref")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--expected-fingerprint")
    return parser


def preview_phone_ban_projection(session, tenant_id: int, batch_id: str) -> dict:
    facts = _facts(session, tenant_id, batch_id, lock=False)
    snapshot = _snapshot(session, facts)
    return {
        "tenant_id": tenant_id,
        "batch_id": batch_id,
        "confirmed_phone_banned_count": len(facts),
        "already_projected_count": sum(item["account_status"] == AccountStatus.BANNED.value for item in snapshot),
        "account_ids": [item["account_id"] for item in snapshot],
        "fingerprint": _fingerprint(snapshot),
    }


def apply_phone_ban_projection(session, args) -> dict:
    _require_apply_args(args)
    if args.actor == args.requested_by:
        raise AuthorizationDrError("approval_actor_conflict", "Phone-ban applier must differ from requester")
    facts = _facts(session, args.tenant_id, args.batch_id, lock=True)
    before = _snapshot(session, facts)
    if _fingerprint(before) != args.expected_fingerprint:
        raise AuthorizationDrError("authorization_version_conflict", "Phone-ban projection facts changed")
    for operation in facts:
        project_authoritative_login_failure(session, operation.account_id, "phone_number_banned")
    audit(
        session,
        tenant_id=args.tenant_id,
        actor=args.actor,
        action="回写 Telegram 手机号封禁事实",
        target_type="tg_authorization_dr_batch",
        target_id=args.batch_id,
        detail=f"count={len(facts)}; approval_ref={args.approval_ref}; idempotency_key={args.idempotency_key}",
    )
    session.commit()
    return preview_phone_ban_projection(session, args.tenant_id, args.batch_id)


def _facts(session, tenant_id: int, batch_id: str, *, lock: bool):
    query = select(TgAuthorizationDrOperation).join(
        TgAuthorizationDrBatchItem,
        TgAuthorizationDrBatchItem.id == TgAuthorizationDrOperation.batch_item_id,
    ).where(
        TgAuthorizationDrOperation.tenant_id == tenant_id,
        TgAuthorizationDrBatchItem.batch_id == batch_id,
        TgAuthorizationDrOperation.blocker_code == "phone_number_banned",
        TgAuthorizationDrOperation.remote_call_state == "confirmed_no_effect",
        TgAuthorizationDrOperation.status == "failed",
    ).order_by(TgAuthorizationDrOperation.account_id)
    return list(session.scalars(query.with_for_update() if lock else query))


def _snapshot(session, facts) -> list[dict]:
    result = []
    for operation in facts:
        account = session.get(TgAccount, operation.account_id)
        if not account or account.deleted_at is not None:
            raise AuthorizationDrError("account_not_found", "Phone-ban fact account is unavailable")
        result.append({
            "operation_id": operation.id,
            "operation_version": operation.operation_version,
            "account_id": operation.account_id,
            "account_status": account.status,
        })
    return result


def _fingerprint(snapshot: list[dict]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_apply_args(args) -> None:
    names = ("actor", "approval_ref", "idempotency_key", "expected_fingerprint")
    missing = [name for name in names if not getattr(args, name)]
    if missing:
        raise AuthorizationDrError("reconcile_approval_required", f"Missing apply arguments: {', '.join(missing)}")


def main() -> int:
    args = _parser().parse_args()
    with SessionLocal() as session:
        if args.mode == "apply":
            result = apply_phone_ban_projection(session, args)
        else:
            result = preview_phone_ban_projection(session, args.tenant_id, args.batch_id)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
