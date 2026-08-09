from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    AccountStatus,
    TgAccount,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
)
from app.schemas.account_security import (
    AccountSecurityBatchCreate,
    AccountSecurityProfileOverride,
    AvatarStrategy,
    ProfileGenerationStrategy,
)
from app.services.account_profile_identity import (
    duplicate_name_groups,
    generate_unique_display_names,
    unavailable_name_keys,
)
from app.services._common import gateway
from app.services.account_security import create_account_security_batch
from app.services.account_usage_policy import apply_operational_account_filters
from app.services.developer_apps import credentials_for_account


MODE = os.getenv("ACCOUNT_PROFILE_DEDUPE_MODE", "preview").strip().lower()
TENANT_ID = int(os.getenv("ACCOUNT_PROFILE_DEDUPE_TENANT_ID", "1"))
SEED = os.getenv("ACCOUNT_PROFILE_DEDUPE_SEED", "").strip()
EXPECTED_SHA256 = os.getenv("ACCOUNT_PROFILE_DEDUPE_EXPECTED_SHA256", "").strip().lower()
DEPLOYED_SHA = os.getenv("ACCOUNT_PROFILE_DEDUPE_DEPLOYED_SHA", "").strip()
APPROVAL_REF = os.getenv("ACCOUNT_PROFILE_DEDUPE_APPROVAL_REF", "").strip()
ACTOR = "github-actions-account-profile-dedupe"
BATCH_SIZE = 50
VALID_MODES = {"preview", "apply", "readback"}


def main() -> int:
    _validate_inputs()
    if MODE == "readback":
        payload = remote_readback()
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        print("ACCOUNT_PROFILE_DUPLICATE_READBACK=" + encoded, flush=True)
        if not payload["complete"]:
            raise RuntimeError("account profile duplicate readback is incomplete")
        return 0
    with SessionLocal() as session:
        manifest = build_manifest(session, tenant_id=TENANT_ID, seed=SEED, deployed_sha=DEPLOYED_SHA)
    manifest_sha = manifest_sha256(manifest)
    batch_ids = _apply(manifest, manifest_sha) if MODE == "apply" else []
    with SessionLocal() as session:
        after = build_manifest(session, tenant_id=TENANT_ID, seed=SEED, deployed_sha=DEPLOYED_SHA)
    payload = {
        "mode": MODE,
        "manifest": manifest,
        "manifest_sha256": manifest_sha,
        "batch_ids": batch_ids,
        "after_duplicate_group_count": after["duplicate_group_count"],
        "after_rename_target_count": after["rename_target_count"],
    }
    print("ACCOUNT_PROFILE_DUPLICATE_RECONCILE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def _validate_inputs() -> None:
    if MODE not in VALID_MODES:
        raise ValueError(f"unsupported mode: {MODE}")
    if not SEED:
        raise ValueError("ACCOUNT_PROFILE_DEDUPE_SEED is required")
    if not DEPLOYED_SHA:
        raise ValueError("ACCOUNT_PROFILE_DEDUPE_DEPLOYED_SHA is required")
    if MODE not in {"apply", "readback"}:
        return
    if not EXPECTED_SHA256:
        raise ValueError("ACCOUNT_PROFILE_DEDUPE_EXPECTED_SHA256 is required for apply/readback")
    if MODE == "apply" and not APPROVAL_REF:
        raise ValueError("ACCOUNT_PROFILE_DEDUPE_APPROVAL_REF is required for apply")


def build_manifest(session, *, tenant_id: int, seed: str, deployed_sha: str) -> dict[str, Any]:
    accounts = _active_accounts(session, tenant_id)
    groups = duplicate_name_groups(accounts)
    target_ids = [account_id for group in groups for account_id in group.target_account_ids]
    generated = generate_unique_display_names(len(target_ids), unavailable_name_keys(session, tenant_id), seed)
    accounts_by_id = {account.id: account for account in accounts}
    targets = [
        {
            "account_id": account_id,
            "old_display_name": accounts_by_id[account_id].display_name,
            "new_display_name": new_name,
            "old_profile_sync_status": accounts_by_id[account_id].profile_sync_status,
            "old_account_status": accounts_by_id[account_id].status,
            "old_account_identity": accounts_by_id[account_id].account_identity,
        }
        for account_id, new_name in zip(target_ids, generated, strict=True)
    ]
    return {
        "tenant_id": tenant_id,
        "deployed_sha": deployed_sha,
        "seed": seed,
        "active_operational_account_count": len(accounts),
        "duplicate_group_count": len(groups),
        "duplicate_account_count": sum(1 + len(group.target_account_ids) for group in groups),
        "rename_target_count": len(targets),
        "keepers": [group.keeper_account_id for group in groups],
        "targets": targets,
    }


def _active_accounts(session, tenant_id: int) -> list[TgAccount]:
    stmt = select(TgAccount).where(
        TgAccount.tenant_id == tenant_id,
        TgAccount.deleted_at.is_(None),
        TgAccount.status == AccountStatus.ACTIVE.value,
        TgAccount.session_ciphertext.is_not(None),
        TgAccount.session_ciphertext != "",
    )
    return list(session.scalars(apply_operational_account_filters(stmt).order_by(TgAccount.id.asc())))


def manifest_sha256(manifest: dict[str, Any]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _apply(manifest: dict[str, Any], manifest_sha: str) -> list[int]:
    if manifest_sha != EXPECTED_SHA256:
        raise RuntimeError(f"manifest hash mismatch: expected={EXPECTED_SHA256};actual={manifest_sha}")
    targets = list(manifest["targets"])
    if not targets:
        raise RuntimeError("apply requires a non-empty exact target set")
    tenant_id = int(manifest["tenant_id"])
    with SessionLocal() as session:
        batch_ids, existing_ids = _existing_batch_state(session, tenant_id, manifest_sha, targets)
        missing = [target for target in targets if int(target["account_id"]) not in existing_ids]
        _assert_unchanged(session, tenant_id, missing)
    for chunk in _chunks(missing, BATCH_SIZE):
        with SessionLocal() as session:
            _assert_unchanged(session, tenant_id, chunk)
            batch = create_account_security_batch(
                session,
                tenant_id,
                _batch_payload(chunk, manifest_sha, len(targets)),
                ACTOR,
            )
            batch_ids.append(int(batch.id))
    return sorted(batch_ids)


def _existing_batch_state(
    session,
    tenant_id: int,
    manifest_sha: str,
    targets: list[dict[str, Any]],
) -> tuple[list[int], set[int]]:
    expected = {int(target["account_id"]): str(target["new_display_name"]) for target in targets}
    stmt = (
        select(TgAccountSecurityBatch, TgAccountSecurityBatchItem)
        .join(TgAccountSecurityBatchItem, TgAccountSecurityBatchItem.batch_id == TgAccountSecurityBatch.id)
        .where(
            TgAccountSecurityBatch.tenant_id == tenant_id,
            TgAccountSecurityBatch.reason.like(_batch_reason(manifest_sha, len(targets)) + "%"),
        )
    )
    batch_ids: set[int] = set()
    account_ids: set[int] = set()
    for batch, item in session.execute(stmt):
        expected_name = expected.get(int(item.account_id))
        if expected_name != item.generated_display_name or int(item.account_id) in account_ids:
            raise RuntimeError(f"existing manifest batch drift: account_id={item.account_id}")
        batch_ids.add(int(batch.id))
        account_ids.add(int(item.account_id))
    return sorted(batch_ids), account_ids


def remote_readback() -> dict[str, Any]:
    with SessionLocal() as session:
        current = build_manifest(session, tenant_id=TENANT_ID, seed=SEED, deployed_sha=DEPLOYED_SHA)
        rows = _readback_rows(session, TENANT_ID, EXPECTED_SHA256)
        results = [_read_remote_profile(session, item, account) for _, item, account in rows]
    batch_ids = sorted({int(batch.id) for batch, _, _ in rows})
    expected_target_count = _readback_target_count(rows)
    terminal_success = bool(rows) and all(result["status"] == "matched" for result in results)
    return {
        "mode": MODE,
        "manifest_sha256": EXPECTED_SHA256,
        "batch_ids": batch_ids,
        "expected_target_count": expected_target_count,
        "target_count": len(rows),
        "remote_matched_count": sum(result["status"] == "matched" for result in results),
        "results": results,
        "after_duplicate_group_count": current["duplicate_group_count"],
        "after_rename_target_count": current["rename_target_count"],
        "complete": terminal_success and len(rows) == expected_target_count and current["rename_target_count"] == 0,
    }


def _readback_rows(session, tenant_id: int, manifest_sha: str) -> list[tuple[Any, Any, TgAccount]]:
    stmt = (
        select(TgAccountSecurityBatch, TgAccountSecurityBatchItem, TgAccount)
        .join(TgAccountSecurityBatchItem, TgAccountSecurityBatchItem.batch_id == TgAccountSecurityBatch.id)
        .join(TgAccount, TgAccount.id == TgAccountSecurityBatchItem.account_id)
        .where(
            TgAccountSecurityBatch.tenant_id == tenant_id,
            TgAccountSecurityBatch.reason.like(f"重复昵称治理 manifest={manifest_sha} target_count=%"),
        )
        .order_by(TgAccountSecurityBatchItem.account_id.asc())
    )
    return list(session.execute(stmt))


def _readback_target_count(rows: list[tuple[Any, Any, TgAccount]]) -> int:
    counts = {
        int(batch.reason.split(" target_count=", 1)[1].split(" ", 1)[0])
        for batch, _, _ in rows
    }
    if len(counts) != 1:
        raise RuntimeError("manifest batch target count is missing or inconsistent")
    return counts.pop()


def _read_remote_profile(session, item: TgAccountSecurityBatchItem, account: TgAccount) -> dict[str, Any]:
    base = {"account_id": int(account.id), "expected_display_name": item.generated_display_name}
    if item.status != "succeeded" or item.profile_status != "succeeded":
        return {**base, "status": "batch_incomplete", "item_status": item.status, "profile_status": item.profile_status}
    try:
        profile = gateway.pull_profile(
            account.id,
            account.session_ciphertext,
            credentials_for_account(session, account),
        )
    except Exception as exc:
        return {**base, "status": "pull_failed", "error_type": type(exc).__name__}
    actual_first_name = profile.first_name or ""
    actual_last_name = profile.last_name or ""
    actual_name = f"{actual_first_name} {actual_last_name}".strip()
    status = "matched" if actual_first_name == item.generated_display_name and not actual_last_name else "mismatched"
    return {**base, "status": status, "actual_display_name": actual_name}


def _assert_unchanged(session, tenant_id: int, targets: list[dict[str, Any]]) -> None:
    target_ids = {int(target["account_id"]) for target in targets}
    accounts = [account for account in _active_accounts(session, tenant_id) if account.id in target_ids]
    current = {account.id: account for account in accounts}
    for target in targets:
        account = current.get(int(target["account_id"]))
        unchanged = account is not None and _target_matches(account, target)
        if not unchanged:
            raise RuntimeError(f"target state drift: account_id={target['account_id']}")


def _target_matches(account: TgAccount, target: dict[str, Any]) -> bool:
    return (
        account.display_name == str(target["old_display_name"])
        and account.profile_sync_status == str(target["old_profile_sync_status"])
        and account.status == str(target["old_account_status"])
        and account.account_identity == str(target["old_account_identity"])
    )


def _batch_payload(targets: list[dict[str, Any]], manifest_sha: str, target_count: int) -> AccountSecurityBatchCreate:
    strategy = ProfileGenerationStrategy(
        generation_mode="local_random",
        language_style="中文",
        persona_style="自然用户",
        bio_enabled=False,
        username_enabled=False,
        overwrite_existing=True,
    )
    return AccountSecurityBatchCreate(
        account_ids=[int(target["account_id"]) for target in targets],
        action_types=["update_profile"],
        confirm_text="确认",
        reason=_batch_reason(manifest_sha, target_count) + APPROVAL_REF,
        profile_strategy=strategy,
        avatar_strategy=AvatarStrategy(mode="none"),
        preview_overrides=[
            AccountSecurityProfileOverride(
                account_id=int(target["account_id"]),
                generated_display_name=str(target["new_display_name"]),
                generated_first_name=str(target["new_display_name"]),
                generated_last_name="",
            )
            for target in targets
        ],
    )


def _batch_reason(manifest_sha: str, target_count: int) -> str:
    return f"重复昵称治理 manifest={manifest_sha} target_count={target_count} approval="


def _chunks(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


if __name__ == "__main__":
    raise SystemExit(main())
