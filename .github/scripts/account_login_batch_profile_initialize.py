from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.image_fingerprint import image_perceptual_hash, perceptual_hash_distance
from app.models import (
    AuditLog,
    TgAccount,
    TgAccountLoginBatchItem,
    TgAccountProfileNameClaim,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
)
from app.schemas.account_security import (
    AccountSecurityBatchCreate,
    AccountSecurityProfileOverride,
    AvatarStrategy,
    ProfileGenerationStrategy,
)
from app.services._common import _now, gateway
from app.services.account_profile_identity import normalize_display_name
from app.services.account_profile_login_batch_init import (
    LoginBatchInitializationSpec,
    build_login_batch_initialization_manifest,
    load_login_batch_targets,
    login_batch_neighbor_scope,
    manifest_sha256,
    target_matches_manifest,
)
from app.services.account_security import activate_account_security_batches, create_account_security_batch
from app.services.developer_apps import credentials_for_account
from app.storage import object_path
from app.timezone import as_beijing_aware


MODE = os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_MODE", "preview").strip().lower()
TENANT_ID = int(os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_TENANT_ID", "1"))
LOGIN_BATCH_IDS = tuple(
    int(value.strip())
    for value in os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_LOGIN_BATCH_IDS", "").split(",")
    if value.strip()
)
CREATED_ONLY_BATCH_IDS = tuple(
    int(value.strip())
    for value in os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_CREATED_ONLY_BATCH_IDS", "").split(",")
    if value.strip()
)
EXPECTED_TARGET_COUNT = int(os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_EXPECTED_TARGET_COUNT", "300"))
STYLE_GROUP_IDS = tuple(
    int(value.strip())
    for value in os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_STYLE_GROUP_IDS", "").split(",")
    if value.strip()
)
STYLE_SAMPLE_CUTOFF_INPUT = os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_STYLE_SAMPLE_CUTOFF_AT", "").strip()
STYLE_SAMPLE_CUTOFF_AT = datetime.fromisoformat(STYLE_SAMPLE_CUTOFF_INPUT) if STYLE_SAMPLE_CUTOFF_INPUT else as_beijing_aware(_now())
SEED = os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_SEED", "").strip()
DEPLOYED_SHA = os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_DEPLOYED_SHA", "").strip().lower()
EXPECTED_SHA256 = os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_EXPECTED_SHA256", "").strip().lower()
APPROVAL_REF = os.getenv("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_APPROVAL_REF", "").strip()
ACTOR = "github-actions-login-batch-profile-init"
BATCH_SIZE = 50
VALID_MODES = {"preview", "apply", "readback"}
OPEN_ITEM_STATUSES = {"pending", "running", "waiting"}
MAX_AVATAR_PERCEPTUAL_DISTANCE = 5
NEIGHBOR_AUDIT_ACTION = "记录账号资料初始化邻居快照"
NEIGHBOR_AUDIT_TARGET_TYPE = "account_profile_init_manifest"


def main() -> int:
    _validate_inputs()
    if MODE == "readback":
        payload = remote_readback()
        _print_payload("ACCOUNT_LOGIN_BATCH_PROFILE_INIT_READBACK", payload)
        if not payload["complete"]:
            raise RuntimeError("login batch profile initialization readback is incomplete")
        return 0
    with SessionLocal() as session:
        manifest = build_login_batch_initialization_manifest(session, _spec())
    actual_sha = manifest_sha256(manifest)
    batch_ids = _apply(manifest, actual_sha) if MODE == "apply" else []
    payload = {
        "mode": MODE,
        "manifest_sha256": actual_sha,
        "login_batch_ids": manifest["login_batch_ids"],
        "created_only_batch_ids": manifest["created_only_batch_ids"],
        "expected_target_count": manifest["expected_target_count"],
        "style": manifest["style"],
        "name_quality": manifest["name_quality"],
        "avatar_pool": manifest["avatar_pool"],
        "target_state_sha256": manifest["target_state_sha256"],
        "batch_ids": batch_ids,
    }
    _print_payload("ACCOUNT_LOGIN_BATCH_PROFILE_INIT", payload)
    return 0


def _spec() -> LoginBatchInitializationSpec:
    return LoginBatchInitializationSpec(
        tenant_id=TENANT_ID,
        login_batch_ids=LOGIN_BATCH_IDS,
        expected_target_count=EXPECTED_TARGET_COUNT,
        style_group_ids=STYLE_GROUP_IDS,
        seed=SEED,
        deployed_sha=DEPLOYED_SHA,
        created_only_batch_ids=CREATED_ONLY_BATCH_IDS,
        style_sample_cutoff_at=STYLE_SAMPLE_CUTOFF_AT,
    )


def _validate_inputs() -> None:
    if MODE not in VALID_MODES:
        raise ValueError(f"unsupported mode: {MODE}; expected preview, apply, or readback")
    if not SEED or not DEPLOYED_SHA or EXPECTED_TARGET_COUNT <= 0:
        raise ValueError("seed, deployed_sha, and a positive expected_target_count are required")
    if len(DEPLOYED_SHA) != 40 or any(char not in "0123456789abcdef" for char in DEPLOYED_SHA):
        raise ValueError("deployed_sha must be the exact 40-character lowercase release SHA")
    if len(set(CREATED_ONLY_BATCH_IDS)) != len(CREATED_ONLY_BATCH_IDS):
        raise ValueError("created_only_batch_ids must be unique")
    if STYLE_SAMPLE_CUTOFF_AT.tzinfo is None:
        raise ValueError("style_sample_cutoff_at must include an explicit timezone")
    if CREATED_ONLY_BATCH_IDS and not set(CREATED_ONLY_BATCH_IDS).issubset(LOGIN_BATCH_IDS):
        raise ValueError("created_only_batch_ids must be a subset of explicit login_batch_ids")
    if MODE == "preview":
        return
    if not STYLE_SAMPLE_CUTOFF_INPUT:
        raise ValueError("apply/readback require style_sample_cutoff_at from preview")
    if not LOGIN_BATCH_IDS or not STYLE_GROUP_IDS:
        raise ValueError("apply/readback require explicit login_batch_ids and style_group_ids from preview")
    if len(EXPECTED_SHA256) != 64:
        raise ValueError("apply/readback require the exact 64-character preview manifest SHA-256")
    if MODE == "apply" and not APPROVAL_REF:
        raise ValueError("apply requires approval_ref")


def _apply(manifest: dict[str, Any], actual_sha: str) -> list[int]:
    if actual_sha != EXPECTED_SHA256:
        raise RuntimeError(f"manifest hash mismatch: expected={EXPECTED_SHA256};actual={actual_sha}")
    targets = list(manifest["targets"])
    if len(targets) != EXPECTED_TARGET_COUNT:
        raise RuntimeError("apply requires the exact approved target count")
    with SessionLocal() as session:
        batch_ids, existing_ids = _existing_manifest_state(session, targets)
        missing = [target for target in targets if int(target["account_id"]) not in existing_ids]
        _assert_no_conflicting_open_items(session, targets, set(batch_ids))
        _assert_targets_unchanged(session, missing)
    for chunk in _chunks(missing, BATCH_SIZE):
        with SessionLocal() as session:
            _assert_targets_unchanged(session, chunk)
            batch = create_account_security_batch(
                session,
                TENANT_ID,
                _batch_payload(chunk, actual_sha),
                ACTOR,
            )
            batch_ids.append(int(batch.id))
    with SessionLocal() as session:
        _assert_targets_unchanged(session, targets, lock=True)
        _assert_no_conflicting_open_items(session, targets, set(batch_ids))
        _ensure_neighbor_scope_audit(session, manifest, actual_sha)
        activate_account_security_batches(
            session,
            TENANT_ID,
            sorted(batch_ids),
            actor=ACTOR,
            confirm_text="确认",
        )
    return sorted(batch_ids)


def _existing_manifest_state(
    session,
    targets: list[dict[str, Any]],
) -> tuple[list[int], set[int]]:
    expected = {
        int(target["account_id"]): (str(target["new_display_name"]), str(target["avatar_source"]))
        for target in targets
    }
    rows = session.execute(
        select(TgAccountSecurityBatch, TgAccountSecurityBatchItem)
        .join(TgAccountSecurityBatchItem, TgAccountSecurityBatchItem.batch_id == TgAccountSecurityBatch.id)
        .where(
            TgAccountSecurityBatch.tenant_id == TENANT_ID,
            TgAccountSecurityBatch.reason.like(_reason_prefix(EXPECTED_SHA256) + "%"),
        )
    )
    batch_ids: set[int] = set()
    account_ids: set[int] = set()
    for batch, item in rows:
        expected_values = expected.get(int(item.account_id))
        actual_values = (item.generated_display_name, item.avatar_source)
        if expected_values != actual_values or int(item.account_id) in account_ids:
            raise RuntimeError(f"existing manifest batch drift: account_id={item.account_id}")
        batch_ids.add(int(batch.id))
        account_ids.add(int(item.account_id))
    return sorted(batch_ids), account_ids


def _assert_no_conflicting_open_items(
    session,
    targets: list[dict[str, Any]],
    allowed_batch_ids: set[int],
) -> None:
    account_ids = [int(target["account_id"]) for target in targets]
    stmt = select(TgAccountSecurityBatchItem).where(
        TgAccountSecurityBatchItem.account_id.in_(account_ids),
        TgAccountSecurityBatchItem.status.in_(OPEN_ITEM_STATUSES),
    )
    if allowed_batch_ids:
        stmt = stmt.where(TgAccountSecurityBatchItem.batch_id.not_in(allowed_batch_ids))
    conflict = session.scalar(stmt.order_by(TgAccountSecurityBatchItem.id.asc()).limit(1))
    if conflict:
        raise RuntimeError(
            f"existing_profile_operation_conflict: account_id={conflict.account_id};batch_id={conflict.batch_id}"
        )


def _assert_targets_unchanged(session, targets: list[dict[str, Any]], *, lock: bool = False) -> None:
    for target in targets:
        account_stmt = select(TgAccount).where(TgAccount.id == int(target["account_id"]))
        item_stmt = select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.id == int(target["login_item_id"]))
        if lock:
            account_stmt = account_stmt.with_for_update()
            item_stmt = item_stmt.with_for_update()
        account = session.scalar(account_stmt)
        item = session.scalar(item_stmt)
        if account is None or item is None or not target_matches_manifest(account, item, target):
            raise RuntimeError(f"target state drift: account_id={target['account_id']}")


def _batch_payload(targets: list[dict[str, Any]], manifest_sha: str) -> AccountSecurityBatchCreate:
    strategy = ProfileGenerationStrategy(
        generation_mode="group_style_v2",
        language_style="中文",
        persona_style="群风格匿名分布",
        bio_enabled=True,
        username_enabled=False,
        overwrite_existing=True,
    )
    return AccountSecurityBatchCreate(
        account_ids=[int(target["account_id"]) for target in targets],
        action_types=["update_profile", "update_avatar"],
        confirm_text="",
        reason=_batch_reason(manifest_sha),
        profile_strategy=strategy,
        avatar_strategy=AvatarStrategy(mode="none"),
        preview_overrides=[_preview_override(target) for target in targets],
    )


def _preview_override(target: dict[str, Any]) -> AccountSecurityProfileOverride:
    return AccountSecurityProfileOverride(
        account_id=int(target["account_id"]),
        generated_display_name=str(target["new_display_name"]),
        generated_first_name=str(target["new_display_name"]),
        generated_last_name="",
        generated_bio=str(target["old_tg_bio"]),
        avatar_source=str(target["avatar_source"]),
    )


def remote_readback() -> dict[str, Any]:
    with SessionLocal() as session:
        rows = _readback_rows(session)
        _assert_readback_target_identity(session, rows)
        results = [_read_remote_result(session, item, account) for _, item, account in rows]
        audit_count = _audit_count(session, rows)
        neighbor_scope = _neighbor_scope_readback(session)
    expected = _readback_expected_count(rows)
    status_counts = _status_counts(rows)
    matched = sum(result["status"] == "matched" for result in results)
    complete = bool(rows) and len(rows) == expected == EXPECTED_TARGET_COUNT and matched == expected
    return {
        "mode": MODE,
        "manifest_sha256": EXPECTED_SHA256,
        "login_batch_ids": list(LOGIN_BATCH_IDS),
        "expected_target_count": expected,
        "target_count": len(rows),
        "remote_matched_count": matched,
        "batch_ids": sorted({int(batch.id) for batch, _, _ in rows}),
        "audit_count": audit_count,
        "neighbor_scope": neighbor_scope,
        "results": results,
        **status_counts,
        "complete": (
            complete
            and audit_count == len({int(batch.id) for batch, _, _ in rows})
            and neighbor_scope["unchanged"]
        ),
    }


def _readback_rows(session) -> list[tuple[Any, TgAccountSecurityBatchItem, TgAccount]]:
    stmt = (
        select(TgAccountSecurityBatch, TgAccountSecurityBatchItem, TgAccount)
        .join(TgAccountSecurityBatchItem, TgAccountSecurityBatchItem.batch_id == TgAccountSecurityBatch.id)
        .join(TgAccount, TgAccount.id == TgAccountSecurityBatchItem.account_id)
        .where(
            TgAccountSecurityBatch.tenant_id == TENANT_ID,
            TgAccountSecurityBatch.reason.like(_reason_prefix(EXPECTED_SHA256) + "%"),
        )
        .order_by(TgAccountSecurityBatchItem.account_id.asc())
    )
    return list(session.execute(stmt))


def _assert_readback_target_identity(session, rows) -> None:
    expected_ids = {int(account.id) for account in load_login_batch_targets(session, _spec()).accounts}
    actual_ids = {int(item.account_id) for _, item, _ in rows}
    if actual_ids != expected_ids or len(actual_ids) != EXPECTED_TARGET_COUNT:
        raise RuntimeError("readback target set does not match the approved login batch")


def _read_remote_result(
    session,
    item: TgAccountSecurityBatchItem,
    account: TgAccount,
) -> dict[str, Any]:
    base = {"account_id": int(account.id), "expected_display_name": item.generated_display_name}
    if item.status != "succeeded" or item.profile_status != "succeeded" or item.avatar_status != "succeeded":
        return {
            **base,
            "status": "batch_incomplete",
            "item_status": item.status,
            "profile_status": item.profile_status,
            "avatar_status": item.avatar_status,
            "failure_type": item.failure_type,
        }
    if not _claim_matches(session, account, item.generated_display_name) or account.display_name != item.generated_display_name:
        return {**base, "status": "persistence_mismatched"}
    local_fingerprint = _local_avatar_fingerprint(account.avatar_object_key)
    if local_fingerprint is None:
        return {**base, "status": "persistence_mismatched", "avatar_object_missing": True}
    try:
        credentials = credentials_for_account(session, account)
        profile = gateway.pull_profile(account.id, account.session_ciphertext, credentials)
        avatar = gateway.pull_profile_avatar_fingerprint(
            account.id,
            session_ciphertext=account.session_ciphertext,
            credentials=credentials,
        )
    except Exception as exc:  # noqa: BLE001 - readback reports typed failure without leaking details.
        return {**base, "status": "pull_failed", "error_type": type(exc).__name__}
    name_matches = profile.first_name == item.generated_display_name and not profile.last_name
    return _avatar_readback_result(base, name_matches, avatar, local_fingerprint)


def _avatar_readback_result(
    base: dict[str, Any],
    name_matches: bool,
    avatar: Any,
    local_fingerprint: dict[str, str],
) -> dict[str, Any]:
    if not name_matches or avatar is None:
        return {**base, "status": "mismatched", "remote_avatar_present": avatar is not None}
    distance = perceptual_hash_distance(local_fingerprint["perceptual_hash"], avatar.perceptual_hash)
    if not avatar.remote_photo_id or distance > MAX_AVATAR_PERCEPTUAL_DISTANCE:
        return {
            **base,
            "status": "mismatched",
            "remote_avatar_present": True,
            "avatar_perceptual_distance": distance,
        }
    return {
        **base,
        "status": "matched",
        "remote_avatar_sha256": avatar.sha256,
        "remote_avatar_size_bytes": avatar.size_bytes,
        "remote_photo_id": avatar.remote_photo_id,
        "remote_avatar_perceptual_hash": avatar.perceptual_hash,
        "local_avatar_sha256": local_fingerprint["sha256"],
        "local_avatar_perceptual_hash": local_fingerprint["perceptual_hash"],
        "avatar_perceptual_distance": distance,
    }


def _claim_matches(session, account: TgAccount, display_name: str) -> bool:
    claim = session.scalar(select(TgAccountProfileNameClaim).where(
        TgAccountProfileNameClaim.tenant_id == account.tenant_id,
        TgAccountProfileNameClaim.name_key == normalize_display_name(display_name),
    ))
    return bool(claim and claim.account_id == account.id)


def _local_avatar_fingerprint(object_key: str) -> dict[str, str] | None:
    if not object_key:
        return None
    path = object_path(object_key)
    if not path.exists() or not path.is_file():
        return None
    data = path.read_bytes()
    return {"sha256": hashlib.sha256(data).hexdigest(), "perceptual_hash": image_perceptual_hash(data)}


def _ensure_neighbor_scope_audit(session, manifest: dict[str, Any], manifest_sha: str) -> None:
    detail = json.dumps(manifest["neighbor_scope"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    audits = list(session.scalars(select(AuditLog).where(
        AuditLog.tenant_id == TENANT_ID,
        AuditLog.action == NEIGHBOR_AUDIT_ACTION,
        AuditLog.target_type == NEIGHBOR_AUDIT_TARGET_TYPE,
        AuditLog.target_id == manifest_sha,
    )))
    if len(audits) > 1 or (audits and audits[0].detail != detail):
        raise RuntimeError("neighbor scope audit drift")
    if not audits:
        session.add(AuditLog(
            tenant_id=TENANT_ID,
            actor=ACTOR,
            action=NEIGHBOR_AUDIT_ACTION,
            target_type=NEIGHBOR_AUDIT_TARGET_TYPE,
            target_id=manifest_sha,
            detail=detail,
        ))


def _neighbor_scope_readback(session) -> dict[str, Any]:
    targets = load_login_batch_targets(session, _spec())
    current = login_batch_neighbor_scope(session, targets)
    audits = list(session.scalars(select(AuditLog).where(
        AuditLog.tenant_id == TENANT_ID,
        AuditLog.action == NEIGHBOR_AUDIT_ACTION,
        AuditLog.target_type == NEIGHBOR_AUDIT_TARGET_TYPE,
        AuditLog.target_id == EXPECTED_SHA256,
    )))
    expected = json.loads(audits[0].detail) if len(audits) == 1 else None
    return {
        "expected_account_count": expected.get("account_count") if expected else None,
        "actual_account_count": current["account_count"],
        "expected_state_sha256": expected.get("state_sha256") if expected else "",
        "actual_state_sha256": current["state_sha256"],
        "unchanged": expected == current,
    }


def _audit_count(session, rows) -> int:
    batch_ids = {str(batch.id) for batch, _, _ in rows}
    if not batch_ids:
        return 0
    return len(list(session.scalars(select(AuditLog.id).where(
        AuditLog.action == "创建账号安全加固批次",
        AuditLog.target_type == "account_security_batch",
        AuditLog.target_id.in_(batch_ids),
    ))))


def _status_counts(rows) -> dict[str, dict[str, int]]:
    return {
        "batch_status_counts": dict(sorted(Counter(batch.status for batch, _, _ in rows).items())),
        "item_status_counts": dict(sorted(Counter(item.status for _, item, _ in rows).items())),
        "profile_status_counts": dict(sorted(Counter(item.profile_status for _, item, _ in rows).items())),
        "avatar_status_counts": dict(sorted(Counter(item.avatar_status for _, item, _ in rows).items())),
        "failure_type_counts": dict(sorted(Counter(item.failure_type for _, item, _ in rows if item.failure_type).items())),
    }


def _readback_expected_count(rows) -> int:
    counts = {
        int(batch.reason.split(" target_count=", 1)[1].split(" ", 1)[0])
        for batch, _, _ in rows
    }
    if len(counts) != 1:
        raise RuntimeError("manifest target count is missing or inconsistent")
    return counts.pop()


def _reason_prefix(manifest_sha: str) -> str:
    batch_ids = ",".join(str(batch_id) for batch_id in LOGIN_BATCH_IDS)
    return (
        f"登录批次资料初始化 manifest={manifest_sha} login_batch_ids={batch_ids} "
        f"target_count={EXPECTED_TARGET_COUNT} approval="
    )


def _batch_reason(manifest_sha: str) -> str:
    reason = _reason_prefix(manifest_sha) + APPROVAL_REF
    if len(reason) > 255:
        raise ValueError("approval_ref is too long for the audited batch reason; use at most 80 characters")
    return reason


def _chunks(values: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def _print_payload(prefix: str, payload: dict[str, Any]) -> None:
    print(prefix + "=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
