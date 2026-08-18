from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AvatarMaterialSource,
    GroupContextMessage,
    Material,
    TgAccount,
    TgAccountLoginBatch,
    TgAccountLoginBatchItem,
    TgGroup,
)
from app.services._common import _now
from app.services.account_profile_identity import normalize_display_name, unavailable_name_keys
from app.services.account_profile_login_batch_targets import (
    LoginBatchInitializationSpec,
    LoginBatchTargets,
    load_login_batch_targets,
    resolve_login_batches,
)
from app.services.account_profile_name_generation import (
    GeneratedDisplayName,
    generate_display_name_candidates,
    name_diversity_metrics,
    style_profile_from_names,
)


STYLE_SAMPLE_DAYS = 30
STYLE_GROUP_MESSAGE_LIMIT = 2_000
MIN_STYLE_SAMPLE_COUNT = 100
MIN_READY_AVATAR_COUNT = 12
MAX_AVATAR_ASSIGNMENT_RATIO = 0.10


@dataclass(frozen=True)
class GroupStyleEvidence:
    group_ids: tuple[int, ...]
    source_name_keys: frozenset[str]
    source_fingerprint: str
    summary: dict[str, Any]
    weights: dict[str, int]


def build_login_batch_initialization_manifest(
    session: Session,
    spec: LoginBatchInitializationSpec,
) -> dict[str, Any]:
    targets = load_login_batch_targets(session, spec)
    style = build_group_style_evidence(session, spec.tenant_id, spec.style_group_ids)
    materials = ready_avatar_materials(session, spec.tenant_id)
    generated = generate_display_name_candidates(
        len(targets.accounts),
        unavailable_name_keys(session, spec.tenant_id),
        spec.seed,
        style_weights=style.weights,
        source_name_keys=set(style.source_name_keys),
    )
    avatars = allocate_avatar_sources(materials, len(targets.accounts), spec.seed)
    target_rows = _target_rows(targets, generated, avatars)
    neighbor_scope = login_batch_neighbor_scope(session, targets)
    manifest = {
        "tenant_id": spec.tenant_id,
        "login_batch_ids": [int(batch.id) for batch in targets.batches],
        "created_only_batch_ids": list(spec.created_only_batch_ids),
        "expected_target_count": spec.expected_target_count,
        "deployed_sha": spec.deployed_sha,
        "seed": spec.seed,
        "login_batches": [_login_batch_snapshot(batch) for batch in targets.batches],
        "style": style.summary,
        "avatar_pool": _avatar_pool_summary(avatars, materials),
        "neighbor_scope": neighbor_scope,
        "name_quality": name_diversity_metrics(generated),
        "targets": target_rows,
    }
    manifest["target_state_sha256"] = _target_state_sha256(target_rows)
    _validate_manifest_quality(manifest)
    return manifest


def build_group_style_evidence(
    session: Session,
    tenant_id: int,
    requested_group_ids: tuple[int, ...],
) -> GroupStyleEvidence:
    group_ids = requested_group_ids or _discover_style_group_ids(session, tenant_id)
    _validate_style_groups(session, tenant_id, group_ids)
    records, names, source_keys, per_group = _style_records(session, tenant_id, group_ids)
    profile = style_profile_from_names(names)
    if profile.sample_count < MIN_STYLE_SAMPLE_COUNT:
        group_value = ",".join(str(group_id) for group_id in group_ids)
        per_group_value = ",".join(
            f"{group_id}:{count}"
            for group_id, count in sorted(per_group.items())
        )
        raise RuntimeError(
            "style_sample_insufficient: "
            f"required={MIN_STYLE_SAMPLE_COUNT};actual={profile.sample_count};"
            f"group_ids={group_value};per_group={per_group_value}"
        )
    encoded = json.dumps(sorted(records), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    summary = {
        "group_ids": list(group_ids),
        "sample_count": profile.sample_count,
        "per_group_sample_count": dict(sorted(per_group.items())),
        "category_counts": dict(profile.category_counts),
        "length_counts": dict(profile.length_counts),
        "source_fingerprint": hashlib.sha256(encoded).hexdigest(),
    }
    return GroupStyleEvidence(
        group_ids=group_ids,
        source_name_keys=frozenset(source_keys),
        source_fingerprint=summary["source_fingerprint"],
        summary=summary,
        weights=profile.weight_map(),
    )


def ready_avatar_materials(session: Session, tenant_id: int) -> list[Material]:
    rows = list(session.scalars(
        select(Material).join(
            AvatarMaterialSource,
            AvatarMaterialSource.material_id == Material.id,
        ).where(
            Material.tenant_id == tenant_id,
            AvatarMaterialSource.tenant_id == tenant_id,
            AvatarMaterialSource.contains_person.is_(False),
            Material.material_type == "图片",
            Material.review_status == "已审核",
            Material.source_kind == "upload",
            Material.mime_type.in_(["image/jpeg", "image/png", "image/webp"]),
            Material.cache_ready_status == "ready",
            Material.tg_cache_account_id.is_not(None),
            Material.tg_cache_peer_id != "",
            Material.tg_cache_message_id != "",
        ).order_by(Material.id.asc())
    ))
    preferred = [row for row in rows if "头像" in f"{row.title} {row.tags}" or "avatar" in f"{row.title} {row.tags}".lower()]
    if len(preferred) < MIN_READY_AVATAR_COUNT:
        raise RuntimeError(
            f"ready_avatar_pool_insufficient: required={MIN_READY_AVATAR_COUNT};actual={len(preferred)}"
        )
    return preferred


def allocate_avatar_sources(materials: list[Material], count: int, seed: str) -> list[str]:
    pool_size = min(len(materials), count)
    pool = materials[:pool_size]
    ordered = sorted(
        pool,
        key=lambda row: (hashlib.sha256(f"{seed}:{row.id}".encode()).hexdigest(), int(row.id)),
    )
    return [f"material:{ordered[index % pool_size].id}" for index in range(count)]


def manifest_sha256(manifest: dict[str, Any]) -> str:
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def target_matches_manifest(account: TgAccount, item: TgAccountLoginBatchItem, target: dict[str, Any]) -> bool:
    return bool(
        item.id == int(target["login_item_id"])
        and item.state_version == int(target["login_item_state_version"])
        and item.status == str(target["login_item_status"])
        and account.display_name == str(target["old_display_name"])
        and account.tg_first_name == str(target["old_tg_first_name"])
        and account.tg_last_name == str(target["old_tg_last_name"])
        and account.profile_sync_status == str(target["old_profile_sync_status"])
        and account.status == str(target["old_account_status"])
        and account.account_identity == str(target["old_account_identity"])
        and account.pool_id == target["old_pool_id"]
        and _avatar_key_sha256(account.avatar_object_key) == str(target["old_avatar_key_sha256"])
    )


def _discover_style_group_ids(session: Session, tenant_id: int) -> tuple[int, ...]:
    return tuple(session.scalars(
        select(TgGroup.id).where(
            TgGroup.tenant_id == tenant_id,
            TgGroup.listener_enabled.is_(True),
        ).order_by(TgGroup.id.asc())
    ))


def _validate_style_groups(session: Session, tenant_id: int, group_ids: tuple[int, ...]) -> None:
    if not group_ids or len(set(group_ids)) != len(group_ids):
        raise RuntimeError("style_group_ids_must_be_nonempty_and_unique")
    matched = set(session.scalars(select(TgGroup.id).where(
        TgGroup.tenant_id == tenant_id,
        TgGroup.id.in_(group_ids),
    )))
    if matched != set(group_ids):
        raise RuntimeError("style_group_not_found_for_tenant")


def _style_records(
    session: Session,
    tenant_id: int,
    group_ids: tuple[int, ...],
) -> tuple[list[tuple[int, str, str]], list[str], set[str], Counter[int]]:
    records: list[tuple[int, str, str]] = []
    names: list[str] = []
    source_keys: set[str] = set()
    per_group: Counter[int] = Counter()
    candidates = {
        group_id: _stable_group_candidates(session, tenant_id, group_id)
        for group_id in group_ids
    }
    for group_id, name, key in _round_robin_samples(candidates, MIN_STYLE_SAMPLE_COUNT):
        profile = style_profile_from_names([name])
        records.append((group_id, profile.category_counts[0][0], profile.length_counts[0][0]))
        names.append(name)
        source_keys.add(key)
        per_group[group_id] += 1
    return records, names, source_keys, per_group


def _stable_group_candidates(
    session: Session,
    tenant_id: int,
    group_id: int,
) -> list[tuple[int, str, str, str]]:
    seen: set[str] = set()
    result: list[tuple[int, str, str, str]] = []
    for row in _stable_group_rows(session, tenant_id, group_id):
        key = normalize_display_name(row.sender_name)
        identity = str(row.sender_peer_id or key)
        if not key or key == normalize_display_name("真人用户") or identity in seen:
            continue
        seen.add(identity)
        result.append((group_id, row.sender_name.strip(), key, identity))
    return result


def _round_robin_samples(
    candidates: dict[int, list[tuple[int, str, str, str]]],
    count: int,
) -> list[tuple[int, str, str]]:
    selected: list[tuple[int, str, str]] = []
    seen_names: set[str] = set()
    seen_identities: set[str] = set()
    index = 0
    while len(selected) < count:
        added = False
        for group_id in candidates:
            if index >= len(candidates[group_id]):
                continue
            added = True
            candidate_group_id, name, key, identity = candidates[group_id][index]
            if key in seen_names or identity in seen_identities:
                continue
            selected.append((candidate_group_id, name, key))
            seen_names.add(key)
            seen_identities.add(identity)
            if len(selected) == count:
                return selected
        if not added:
            return selected
        index += 1
    return selected


def _stable_group_rows(session: Session, tenant_id: int, group_id: int) -> list[GroupContextMessage]:
    cutoff = _now() - timedelta(days=STYLE_SAMPLE_DAYS)
    return list(session.scalars(
        select(GroupContextMessage).where(
            GroupContextMessage.tenant_id == tenant_id,
            GroupContextMessage.group_id == group_id,
            GroupContextMessage.is_bot.is_(False),
            GroupContextMessage.sender_name != "",
            GroupContextMessage.created_at >= cutoff,
        ).order_by(GroupContextMessage.created_at.asc(), GroupContextMessage.id.asc()).limit(STYLE_GROUP_MESSAGE_LIMIT)
    ))


def _target_rows(
    targets: LoginBatchTargets,
    generated: list[GeneratedDisplayName],
    avatars: list[str],
) -> list[dict[str, Any]]:
    return [
        _target_row(
            item,
            account,
            generated_item,
            avatar_source=avatar_source,
        )
        for item, account, generated_item, avatar_source in zip(
            targets.items,
            targets.accounts,
            generated,
            avatars,
            strict=True,
        )
    ]


def _target_row(
    item: TgAccountLoginBatchItem,
    account: TgAccount,
    generated: GeneratedDisplayName,
    *,
    avatar_source: str,
) -> dict[str, Any]:
    return {
        "account_id": int(account.id),
        "login_batch_id": int(item.batch_id),
        "login_item_id": int(item.id),
        "login_item_state_version": int(item.state_version),
        "login_item_status": item.status,
        "old_display_name": account.display_name,
        "old_tg_first_name": account.tg_first_name,
        "old_tg_last_name": account.tg_last_name,
        "old_tg_bio": account.tg_bio,
        "old_avatar_key_sha256": _avatar_key_sha256(account.avatar_object_key),
        "old_profile_sync_status": account.profile_sync_status,
        "old_account_status": account.status,
        "old_account_identity": account.account_identity,
        "old_pool_id": account.pool_id,
        "new_display_name": generated.display_name,
        "name_category": generated.category,
        "avatar_source": avatar_source,
    }


def _login_batch_snapshot(batch: TgAccountLoginBatch) -> dict[str, Any]:
    return {
        "status": batch.status,
        "total_count": batch.total_count,
        "success_count": batch.success_count,
        "unresolved_count": batch.unresolved_count,
        "state_version": batch.state_version,
        "execution_generation": batch.execution_generation,
        "resolution_version": batch.resolution_version,
        "finished_at": batch.finished_at.isoformat() if batch.finished_at else "",
    }


def _avatar_pool_summary(sources: list[str], materials: list[Material]) -> dict[str, Any]:
    counts = Counter(sources)
    material_ids = sorted(int(source.removeprefix("material:")) for source in counts)
    material_by_id = {int(material.id): material for material in materials}
    pool_state = [
        _avatar_material_state(material_by_id[material_id])
        for material_id in material_ids
    ]
    return {
        "ready_material_count": len(material_ids),
        "material_ids": material_ids,
        "assignment_counts": dict(sorted(counts.items())),
        "unique_avatar_material_count": len(counts),
        "max_material_assignment_count": max(counts.values(), default=0),
        "pool_state_sha256": _canonical_sha256(pool_state),
    }


def _avatar_material_state(material: Material) -> dict[str, Any]:
    return {
        "material_id": int(material.id),
        "asset_version_id": int(material.asset_version_id),
        "tg_ref_version_id": int(material.tg_ref_version_id),
        "asset_fingerprint": material.asset_fingerprint,
        "tg_cache_account_id": material.tg_cache_account_id,
        "tg_cache_peer_id": material.tg_cache_peer_id,
        "tg_cache_message_id": material.tg_cache_message_id,
    }


def login_batch_neighbor_scope(session: Session, targets: LoginBatchTargets) -> dict[str, Any]:
    target_ids = {int(account.id) for account in targets.accounts}
    batch_ids = [int(batch.id) for batch in targets.batches]
    account_ids = set(session.scalars(select(TgAccountLoginBatchItem.account_id).where(
        TgAccountLoginBatchItem.batch_id.in_(batch_ids),
        TgAccountLoginBatchItem.account_id.is_not(None),
    ))) - target_ids
    accounts = {
        int(account.id): account
        for account in session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)))
    }
    rows = [_neighbor_state(account_id, accounts.get(account_id)) for account_id in sorted(account_ids)]
    return {"account_count": len(rows), "state_sha256": _canonical_sha256(rows)}


def _neighbor_state(account_id: int, account: TgAccount | None) -> dict[str, Any]:
    if account is None:
        return {"account_id": account_id, "missing": True}
    return {
        "account_id": account_id,
        "display_name": account.display_name,
        "tg_first_name": account.tg_first_name,
        "tg_last_name": account.tg_last_name,
        "tg_bio": account.tg_bio,
        "avatar_object_key_sha256": _avatar_key_sha256(account.avatar_object_key),
        "username": account.username,
    }


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_manifest_quality(manifest: dict[str, Any]) -> None:
    target_count = len(manifest["targets"])
    if target_count != int(manifest["expected_target_count"]):
        raise RuntimeError("manifest_target_count_mismatch")
    category_counts = manifest["name_quality"]["category_counts"]
    if len(category_counts) < 8 or max(category_counts.values(), default=0) > target_count * 0.25:
        raise RuntimeError("name_category_distribution_failed")
    if set(manifest["name_quality"]["length_counts"]) != {"short_2_3", "medium_4_6", "long_7_12"}:
        raise RuntimeError("name_length_distribution_failed")
    avatar_max = int(manifest["avatar_pool"]["max_material_assignment_count"])
    if avatar_max > max(1, target_count * MAX_AVATAR_ASSIGNMENT_RATIO):
        raise RuntimeError("avatar_assignment_distribution_failed")


def _target_state_sha256(targets: list[dict[str, Any]]) -> str:
    state = [
        {key: value for key, value in target.items() if key not in {"new_display_name", "name_category", "avatar_source"}}
        for target in targets
    ]
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _avatar_key_sha256(value: str | None) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


__all__ = [
    "LoginBatchInitializationSpec",
    "allocate_avatar_sources",
    "build_group_style_evidence",
    "build_login_batch_initialization_manifest",
    "load_login_batch_targets",
    "login_batch_neighbor_scope",
    "manifest_sha256",
    "ready_avatar_materials",
    "resolve_login_batches",
    "target_matches_manifest",
]
