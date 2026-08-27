from __future__ import annotations

import hashlib
import json
from datetime import timedelta

from sqlalchemy import select

from app.image_fingerprint import image_avatar_perceptual_hash, perceptual_hash_distance
from app.models import (
    Tenant,
    TgAccount,
    TgAccountFullInitialization,
    TgAccountProfileNameClaim,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
    TgAccountSecuritySnapshot,
)
from app.schemas.account_security import (
    AccountSecurityBatchCreate,
    AccountSecurityProfileOverride,
    AvatarStrategy,
    ProfileGenerationStrategy,
)
from app.services._common import _now, audit, gateway
from app.services.account_profile_identity import normalize_display_name
from app.services.account_security import (
    account_security_batch_detail,
    create_account_security_batch,
)
from app.services.developer_apps import credentials_for_account
from app.storage import object_path

from .contracts import FullInitializationClaim
from .flow import advance_full_initialization
from .profile_gap import (
    ProfileGapReadback,
    freeze_completed_target,
    freeze_created_target,
    gap_actions,
    requested_actions,
    target_from_owner,
)


PROFILE_POLL_SECONDS = 5
MAX_AVATAR_PERCEPTUAL_DISTANCE = 5


def execute_profile_stage(session_factory, claim: FullInitializationClaim) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        if owner.profile_batch_id is None:
            target = target_from_owner(owner)
        else:
            target = None
    if target is not None:
        readback = _current_profile_gaps(session_factory, claim, target)
        if readback is None:
            return
        if not readback.actions:
            _finish_existing_profile(session_factory, claim, readback)
            return
        _record_profile_actions(session_factory, claim, readback.actions)
    _continue_profile_stage(session_factory, claim)


def _continue_profile_stage(session_factory, claim) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        if owner.profile_batch_id is None:
            _create_profile_batch(session_factory, claim, owner)
            return
        item = _profile_item(session, owner)
        if not item:
            _finish_profile_failure(owner, item)
            session.commit()
            return
        if item.status in {"pending", "running", "waiting"}:
            _wait_for_profile(owner)
            session.commit()
            return
        if not _batch_item_succeeded(item, owner.profile_action_types):
            _finish_profile_failure(owner, item)
            session.commit()
            return
        account = _validated_account(session, owner)
        freeze_completed_target(owner, account, item)
        expected_name = owner.profile_target_name
        local_fingerprint = _local_avatar_fingerprint(owner.profile_target_avatar_object_key)
        if not _local_projection_matches(
            session,
            account,
            expected_name=expected_name,
            fingerprint=local_fingerprint,
        ):
            _mark_failed(owner, "profile_persistence_mismatch", "平台姓名或头像投影不匹配")
            session.commit()
            return
        credentials = credentials_for_account(session, account)
        session_ciphertext = account.session_ciphertext
        session.commit()
    try:
        profile, avatar = _pull_remote_profile(account.id, session_ciphertext=session_ciphertext, credentials=credentials)
    except Exception as exc:
        _finish_readback_failure(session_factory, claim, exc)
        return
    _commit_profile_readback(
        session_factory,
        claim,
        expected_name=expected_name,
        profile=profile,
        avatar=avatar,
        local_fingerprint=local_fingerprint,
    )


def _commit_profile_readback(
    session_factory,
    claim,
    *,
    expected_name,
    profile,
    avatar,
    local_fingerprint,
) -> None:
    try:
        _finish_profile_readback(
            session_factory,
            claim,
            expected_name=expected_name,
            profile=profile,
            avatar=avatar,
            local_fingerprint=local_fingerprint,
        )
    except Exception as exc:
        _finish_readback_failure(session_factory, claim, exc)


def _pull_remote_profile(account_id: int, *, session_ciphertext: str, credentials):
    profile = gateway.pull_profile(account_id, session_ciphertext, credentials)
    avatar = gateway.pull_profile_avatar_fingerprint(
        account_id,
        session_ciphertext=session_ciphertext,
        credentials=credentials,
    )
    return profile, avatar


def _current_profile_gaps(session_factory, claim, target):
    with session_factory() as session:
        owner = _load_claim(session, claim)
        account = _validated_account(session, owner)
        credentials = credentials_for_account(session, account)
        local_fingerprint = _local_avatar_fingerprint(target.avatar_object_key)
        local_name_matches = _local_name_matches(session, account, target.name)
        local_avatar_matches = account.avatar_object_key == target.avatar_object_key
        session_ciphertext = account.session_ciphertext
        account_id = account.id
    try:
        profile, avatar = _pull_remote_profile(
            account_id,
            session_ciphertext=session_ciphertext,
            credentials=credentials,
        )
    except Exception as exc:
        _finish_readback_failure(session_factory, claim, exc)
        return None
    name_matches = local_name_matches and profile.first_name == target.name and not profile.last_name
    avatar_matches = local_avatar_matches and _avatar_matches(avatar, local_fingerprint)
    return ProfileGapReadback(
        gap_actions(name_matches=name_matches, avatar_matches=avatar_matches),
        target,
        profile,
        avatar,
        local_fingerprint,
    )


def _finish_existing_profile(session_factory, claim, readback) -> None:
    _commit_profile_readback(
        session_factory,
        claim,
        expected_name=readback.target.name,
        profile=readback.profile,
        avatar=readback.avatar,
        local_fingerprint=readback.local_fingerprint,
    )


def _record_profile_actions(session_factory, claim, actions) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        owner.profile_action_types = json.dumps(list(actions), separators=(",", ":"))
        owner.version += 1
        session.commit()


def _create_profile_batch(session_factory, claim, owner) -> None:
    actions = requested_actions(owner.profile_action_types)
    try:
        with session_factory() as create_session:
            batch = _existing_profile_batch(create_session, owner)
            batch = batch or create_account_security_batch(
                create_session, owner.tenant_id,
                _profile_payload(owner.account_id, actions=actions, owner=owner),
                owner.execution_owner, idempotency_key=_profile_key(owner.id),
            )
            batch_id = batch.id
    except Exception as exc:
        _finish_profile_create_failure(session_factory, claim, exc)
        return
    with session_factory() as session:
        current = _load_claim(session, claim)
        current.profile_batch_id = batch_id
        profile_item = _profile_item(session, current)
        if profile_item:
            current.profile_item_id = profile_item.id
            freeze_created_target(current, profile_item, actions)
        _wait_for_profile(current)
        session.commit()


def _existing_profile_batch(session, owner):
    batch = session.scalar(
        select(TgAccountSecurityBatch).where(
            TgAccountSecurityBatch.tenant_id == owner.tenant_id,
            TgAccountSecurityBatch.idempotency_key == _profile_key(owner.id),
        )
    )
    if not batch:
        return None
    return account_security_batch_detail(session, owner.tenant_id, batch.id)


def _profile_key(owner_id: int) -> str:
    return f"post-login-profile:{owner_id}"


def _profile_payload(
    account_id: int,
    *,
    actions,
    owner: TgAccountFullInitialization,
) -> AccountSecurityBatchCreate:
    return AccountSecurityBatchCreate(
        account_ids=[account_id],
        action_types=list(actions),
        confirm_text="确认",
        reason="批量登录后初始化账号姓名和头像",
        profile_strategy=ProfileGenerationStrategy(
            generation_mode="local_random",
            language_style="中文",
            persona_style="自然用户",
            bio_enabled=False,
            username_enabled=False,
            overwrite_existing=True,
        ),
        avatar_strategy=AvatarStrategy(mode="material_random"),
        preview_overrides=_profile_overrides(account_id, owner),
    )


def _profile_overrides(
    account_id: int,
    owner: TgAccountFullInitialization,
) -> list[AccountSecurityProfileOverride]:
    if not owner.profile_target_name:
        return []
    return [AccountSecurityProfileOverride(
        account_id=account_id,
        generated_display_name=owner.profile_target_name,
        generated_first_name=owner.profile_target_name,
        avatar_source=owner.profile_target_avatar_source or "",
    )]


def _finish_profile_readback(
    session_factory,
    claim,
    *,
    expected_name: str,
    profile,
    avatar,
    local_fingerprint,
) -> None:
    name_matches = profile.first_name == expected_name and not profile.last_name
    avatar_matches = _avatar_matches(avatar, local_fingerprint)
    with session_factory() as session:
        owner = _load_claim(session, claim)
        if not name_matches or not avatar_matches:
            _mark_failed(owner, "profile_remote_mismatch", "Telegram 姓名或头像读回不匹配")
            session.commit()
            return
        owner.profile_status = "succeeded"
        owner.profile_evidence_ref = f"full-init:{owner.id}:profile"
        advance_full_initialization(owner)
        _mark_snapshot_profile_complete(session, owner.account_id)
        audit(
            session,
            tenant_id=owner.tenant_id,
            actor=owner.execution_owner,
            action="完成批量登录姓名头像初始化",
            target_type="tg_account_full_initialization",
            target_id=str(owner.id),
            detail=owner.profile_evidence_ref,
        )
        session.commit()


def _profile_item(session, owner) -> TgAccountSecurityBatchItem | None:
    batch = session.get(TgAccountSecurityBatch, owner.profile_batch_id)
    if not batch or batch.tenant_id != owner.tenant_id:
        return None
    return session.scalar(
        select(TgAccountSecurityBatchItem).where(
            TgAccountSecurityBatchItem.batch_id == batch.id,
            TgAccountSecurityBatchItem.account_id == owner.account_id,
        )
    )


def _batch_item_succeeded(item, raw_actions: str) -> bool:
    actions = set(requested_actions(raw_actions))
    statuses = []
    if "update_profile" in actions:
        statuses.append(item.profile_status)
    if "update_avatar" in actions:
        statuses.append(item.avatar_status)
    return item.status == "succeeded" and statuses and all(value == "succeeded" for value in statuses)


def _local_projection_matches(session, account, *, expected_name: str, fingerprint) -> bool:
    return bool(
        _local_name_matches(session, account, expected_name)
        and account.avatar_object_key
        and fingerprint
    )


def _local_name_matches(session, account, expected_name: str) -> bool:
    claim = session.scalar(
        select(TgAccountProfileNameClaim).where(
            TgAccountProfileNameClaim.tenant_id == account.tenant_id,
            TgAccountProfileNameClaim.name_key == normalize_display_name(expected_name),
        )
    )
    return bool(
        claim
        and claim.account_id == account.id
        and account.display_name == expected_name
        and account.tg_first_name == expected_name
        and not account.tg_last_name
    )


def _local_avatar_fingerprint(object_key: str):
    if not object_key:
        return None
    path = object_path(object_key)
    if not path.exists() or not path.is_file():
        return None
    data = path.read_bytes()
    return {
        "sha256": hashlib.sha256(data).hexdigest(),
        "perceptual_hash": image_avatar_perceptual_hash(data),
    }


def _avatar_matches(avatar, local_fingerprint) -> bool:
    if avatar is None or local_fingerprint is None or not avatar.remote_photo_id:
        return False
    distance = perceptual_hash_distance(
        local_fingerprint["perceptual_hash"],
        avatar.perceptual_hash,
    )
    return distance <= MAX_AVATAR_PERCEPTUAL_DISTANCE


def _wait_for_profile(owner) -> None:
    owner.status = "waiting_profile"
    owner.profile_status = "running"
    owner.next_retry_at = _now() + timedelta(seconds=PROFILE_POLL_SECONDS)
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _finish_profile_failure(owner, item) -> None:
    detail = item.failure_detail if item else "profile batch item unavailable"
    code = item.failure_type if item and item.failure_type else "profile_initialization_failed"
    if item and (item.profile_status == "skipped" or item.avatar_status == "skipped"):
        _mark_manual(owner, "profile_material_unavailable", detail)
        return
    _mark_failed(owner, code, detail)


def _finish_profile_create_failure(session_factory, claim, exc) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        owner.status = "manual_required"
        owner.stage = "manual_required"
        owner.profile_status = "manual_required"
        owner.failure_type = "profile_prerequisite_unavailable"
        owner.failure_detail = type(exc).__name__
        owner.finished_at = _now()
        owner.lease_token = ""
        owner.lease_expires_at = None
        owner.version += 1
        session.commit()


def _finish_readback_failure(session_factory, claim, exc) -> None:
    with session_factory() as session:
        owner = _load_claim(session, claim)
        owner.status = "reconcile_unknown"
        owner.stage = "reconcile_unknown"
        owner.profile_status = "reconcile_unknown"
        owner.failure_type = "profile_readback_unknown"
        owner.failure_detail = type(exc).__name__
        owner.finished_at = _now()
        owner.lease_token = ""
        owner.lease_expires_at = None
        owner.version += 1
        session.commit()


def _mark_failed(owner, code: str, detail: str) -> None:
    owner.status = "failed"
    owner.stage = "failed"
    owner.profile_status = "failed"
    owner.failure_type = code[:100]
    owner.failure_detail = detail[:500]
    owner.finished_at = _now()
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _mark_manual(owner, code: str, detail: str) -> None:
    owner.status = "manual_required"
    owner.stage = "manual_required"
    owner.profile_status = "manual_required"
    owner.failure_type = code[:100]
    owner.failure_detail = detail[:500]
    owner.finished_at = _now()
    owner.lease_token = ""
    owner.lease_expires_at = None
    owner.version += 1


def _validated_account(session, owner) -> TgAccount:
    account = session.get(TgAccount, owner.account_id)
    tenant = session.get(Tenant, owner.tenant_id)
    if not account or not tenant or account.deleted_at is not None:
        raise RuntimeError("post-login profile account is unavailable")
    if account.account_identity != "normal":
        raise RuntimeError("post-login profile account usage changed")
    if account.authorization_generation != owner.authorization_generation:
        raise RuntimeError("post-login profile A generation changed")
    if tenant.fixed_two_fa_password_version != owner.fixed_two_fa_version:
        raise RuntimeError("post-login profile fixed 2FA policy changed")
    return account


def _load_claim(session, claim) -> TgAccountFullInitialization:
    owner = session.get(TgAccountFullInitialization, claim.initialization_id)
    if not owner or owner.lease_token != claim.lease_token or owner.stage != claim.stage:
        raise RuntimeError("post-login initialization claim is stale")
    return owner


def _mark_snapshot_profile_complete(session, account_id: int) -> None:
    snapshot = session.scalar(
        select(TgAccountSecuritySnapshot).where(
            TgAccountSecuritySnapshot.account_id == account_id
        )
    )
    if snapshot:
        snapshot.profile_status = "complete"
        snapshot.profile_last_updated_at = _now()


__all__ = ["execute_profile_stage"]
