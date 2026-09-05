from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.comment_fallback_policy import validate_comment_fallback_policy
from app.models import (
    ChannelCommentFallbackPoolSnapshot,
    CommentFallbackPolicySnapshot,
    CommentFallbackSelection,
    ExecutionAttempt,
    FallbackShuffleBagCursor,
    MaterialGroup,
    Task,
)

from .comment_fallback_contract import (
    CommentFallbackUnavailable,
    FALLBACK_POLICY_VERSION,
    MATERIAL_CONTRACT_VERSION,
    SelectedCommentFallback,
    UNICODE_ALLOWLIST_HASH,
    UNICODE_ALLOWLIST_VERSION,
    UNICODE_EMOJI_ALLOWLIST_V2,
)
from .comment_fallback_materials import (
    asset_manifest,
    ready_image_assets,
    selected_image_available,
)
from .comment_fallback_projection import selection_result


def validate_comment_fallback_config(
    session: Session,
    tenant_id: int,
    config: dict,
) -> None:
    if not config.get("channel_comment_grounding_v1_enabled"):
        return
    validate_comment_fallback_policy(config)
    if int(config.get("image_meme_weight_bps", 0) or 0) > 0:
        _require_ready_material_group(session, tenant_id, config)


def freeze_comment_fallback_contract(
    session: Session,
    task: Task,
    *,
    channel_message_id: int,
    comment_plan_revision: int,
    content_mix_contract_id: str,
) -> ChannelCommentFallbackPoolSnapshot | None:
    config = dict(task.type_config or {})
    if not config.get("channel_comment_grounding_v1_enabled"):
        return None
    existing = _pool_snapshot(session, content_mix_contract_id)
    if existing:
        return existing
    policy = _policy_snapshot(
        session,
        task,
        config,
        task_config_revision=comment_plan_revision,
    )
    if policy is None:
        return None
    assets = ready_image_assets(session, task.tenant_id, policy.image_meme_material_group_id)
    manifest = [asset_manifest(item) for item in assets]
    row = ChannelCommentFallbackPoolSnapshot(
        tenant_id=task.tenant_id,
        task_id=task.id,
        channel_message_id=channel_message_id,
        comment_plan_revision=comment_plan_revision,
        content_mix_contract_id=content_mix_contract_id,
        fallback_policy_snapshot_id=policy.id,
        image_meme_assets=manifest,
        image_meme_asset_pool_hash=_stable_hash(manifest),
        pool_state="ready" if manifest else "fallback_material_pool_empty",
    )
    session.add(row)
    session.flush()
    _precreate_shuffle_cursors(session, row, policy, manifest)
    return row


def _precreate_shuffle_cursors(
    session: Session,
    pool: ChannelCommentFallbackPoolSnapshot,
    policy: CommentFallbackPolicySnapshot,
    image_assets: list[dict],
) -> None:
    items_by_kind = {
        "unicode_emoji": list(UNICODE_EMOJI_ALLOWLIST_V2) if policy.unicode_enabled else [],
        "image_meme": image_assets if policy.image_meme_enabled else [],
    }
    for content_kind, items in items_by_kind.items():
        if not items:
            continue
        ordered, seed = _stable_bag(pool, policy, content_kind, items)
        _locked_cursor(session, pool, content_kind, seed, ordered)


def select_comment_fallback(
    session: Session,
    *,
    action_id: str,
    tenant_id: int,
    task_id: str,
    content_mix_contract_id: str,
    target_ordinal: int,
    fallback_reason: str,
    fallback_kind: str = "emergency",
) -> SelectedCommentFallback:
    if fallback_kind not in {"planned", "emergency"}:
        raise CommentFallbackUnavailable("fallback_kind_invalid")
    pool, policy = _locked_fallback_contract(
        session,
        tenant_id=tenant_id,
        task_id=task_id,
        content_mix_contract_id=content_mix_contract_id,
    )
    current = _latest_selection(session, content_mix_contract_id, target_ordinal)
    if current and current.fallback_kind != fallback_kind:
        raise CommentFallbackUnavailable("fallback_kind_identity_mismatch")
    if current and current.selection_state == "ready":
        if current.fallback_content_kind == "unicode_emoji":
            return selection_result(current)
        if selected_image_available(session, current):
            return selection_result(current)
        if _gateway_started(session, action_id):
            raise CommentFallbackUnavailable("fallback_selection_locked_after_gateway")
        current.selection_state = "material_unavailable"
    selected_kind = current.fallback_content_kind if current else _select_kind(
        policy, task_id=task_id, pool=pool, target_ordinal=target_ordinal,
    )
    attempt = int(current.selection_attempt if current else 0) + 1
    try:
        row = _select_ready_row(
            session, policy=policy, pool=pool, target_ordinal=target_ordinal,
            content_kind=selected_kind, attempt=attempt, fallback_reason=fallback_reason,
            fallback_kind=fallback_kind,
        )
    except CommentFallbackUnavailable:
        session.commit()
        raise
    session.commit()
    return selection_result(row)


def _locked_fallback_contract(
    session: Session,
    *,
    tenant_id: int,
    task_id: str,
    content_mix_contract_id: str,
) -> tuple[ChannelCommentFallbackPoolSnapshot, CommentFallbackPolicySnapshot]:
    pool = session.scalar(
        select(ChannelCommentFallbackPoolSnapshot)
        .where(
            ChannelCommentFallbackPoolSnapshot.content_mix_contract_id
            == content_mix_contract_id,
            ChannelCommentFallbackPoolSnapshot.tenant_id == tenant_id,
            ChannelCommentFallbackPoolSnapshot.task_id == task_id,
        )
        .with_for_update()
    )
    if pool is None:
        raise CommentFallbackUnavailable("fallback_contract_missing")
    policy = session.get(CommentFallbackPolicySnapshot, pool.fallback_policy_snapshot_id)
    if policy is None:
        raise CommentFallbackUnavailable("fallback_policy_snapshot_missing")
    return pool, policy


def _policy_snapshot(
    session: Session,
    task: Task,
    config: dict,
    *,
    task_config_revision: int,
) -> CommentFallbackPolicySnapshot | None:
    revision = int(task_config_revision)
    existing = session.scalar(select(CommentFallbackPolicySnapshot).where(
        CommentFallbackPolicySnapshot.task_id == task.id,
        CommentFallbackPolicySnapshot.task_config_revision == revision,
    ))
    if existing:
        return existing
    if int(task.config_revision or 1) != revision:
        raise CommentFallbackUnavailable(
            "fallback_policy_snapshot_missing_for_plan_revision"
        )
    if not validate_comment_fallback_policy(config):
        return None
    row = CommentFallbackPolicySnapshot(
        tenant_id=task.tenant_id, task_id=task.id, task_config_revision=revision,
        fallback_policy_version=FALLBACK_POLICY_VERSION,
        unicode_allowlist_version=UNICODE_ALLOWLIST_VERSION,
        unicode_allowlist_hash=UNICODE_ALLOWLIST_HASH,
        unicode_enabled=bool(config.get("unicode_emoji_enabled", True)),
        image_meme_enabled=bool(config.get("image_meme_enabled", False)),
        image_meme_material_group_id=config.get("image_meme_material_group_id"),
        unicode_weight_bps=int(config.get("unicode_emoji_weight_bps", 10000) or 0),
        image_meme_weight_bps=int(config.get("image_meme_weight_bps", 0) or 0),
        allow_image_reselection_before_gateway=bool(
            config.get("allow_image_reselection_before_gateway", True)
        ),
        allow_cross_kind_fallback_to_unicode=bool(
            config.get("allow_cross_kind_fallback_to_unicode", True)
        ),
        material_contract_version=MATERIAL_CONTRACT_VERSION,
    )
    session.add(row)
    session.flush()
    return row


def _select_ready_row(
    session: Session,
    *,
    policy: CommentFallbackPolicySnapshot,
    pool: ChannelCommentFallbackPoolSnapshot,
    target_ordinal: int,
    content_kind: str,
    attempt: int,
    fallback_reason: str,
    fallback_kind: str,
) -> CommentFallbackSelection:
    if content_kind == "unicode_emoji":
        if not policy.unicode_enabled:
            raise CommentFallbackUnavailable("fallback_unicode_disabled")
        return _consume_selection(
            session, policy=policy, pool=pool, target_ordinal=target_ordinal,
            content_kind=content_kind, attempt=attempt,
            fallback_reason=fallback_reason, fallback_kind=fallback_kind,
            items=list(UNICODE_EMOJI_ALLOWLIST_V2),
        )
    return _select_image_row(
        session, policy=policy, pool=pool, target_ordinal=target_ordinal,
        attempt=attempt, fallback_reason=fallback_reason,
        fallback_kind=fallback_kind,
    )


def _select_image_row(
    session: Session,
    *,
    policy: CommentFallbackPolicySnapshot,
    pool: ChannelCommentFallbackPoolSnapshot,
    target_ordinal: int,
    attempt: int,
    fallback_reason: str,
    fallback_kind: str,
) -> CommentFallbackSelection:
    assets = list(pool.image_meme_assets or [])
    consumed = 0
    if policy.image_meme_enabled and assets:
        limit = len(assets) if policy.allow_image_reselection_before_gateway else 1
        for offset in range(limit):
            consumed += 1
            row = _consume_selection(
                session, policy=policy, pool=pool, target_ordinal=target_ordinal,
                content_kind="image_meme", attempt=attempt + offset,
                fallback_reason=fallback_reason, fallback_kind=fallback_kind,
                items=assets,
            )
            if selected_image_available(session, row):
                return row
            row.selection_state = "material_unavailable"
    if policy.allow_cross_kind_fallback_to_unicode and policy.unicode_enabled:
        return _consume_selection(
            session, policy=policy, pool=pool, target_ordinal=target_ordinal,
            content_kind="unicode_emoji", attempt=attempt + consumed,
            fallback_reason="image_meme_unavailable_unicode_fallback",
            fallback_kind=fallback_kind,
            items=list(UNICODE_EMOJI_ALLOWLIST_V2),
        )
    raise CommentFallbackUnavailable("fallback_material_shortfall")


def _consume_selection(
    session: Session,
    *,
    policy: CommentFallbackPolicySnapshot,
    pool: ChannelCommentFallbackPoolSnapshot,
    target_ordinal: int,
    content_kind: str,
    attempt: int,
    fallback_reason: str,
    fallback_kind: str,
    items: list,
) -> CommentFallbackSelection:
    if not items:
        raise CommentFallbackUnavailable("fallback_pool_exhausted")
    ordered, seed = _stable_bag(pool, policy, content_kind, items)
    cursor = _locked_cursor(session, pool, content_kind, seed, ordered)
    rank = int(cursor.next_rank or 0)
    cycle = int(cursor.cycle or 0)
    item = ordered[rank]
    cursor.next_rank = rank + 1
    if cursor.next_rank >= len(ordered):
        cursor.next_rank = 0
        cursor.cycle = cycle + 1
    cursor.cursor_version = int(cursor.cursor_version or 0) + 1
    row = _selection_row(
        policy=policy, pool=pool, target_ordinal=target_ordinal,
        content_kind=content_kind, attempt=attempt, fallback_reason=fallback_reason,
        fallback_kind=fallback_kind, seed=seed, cycle=cycle, rank=rank, item=item,
    )
    session.add(row)
    session.flush()
    return row


def _selection_row(
    *,
    policy: CommentFallbackPolicySnapshot,
    pool: ChannelCommentFallbackPoolSnapshot,
    target_ordinal: int,
    content_kind: str,
    attempt: int,
    fallback_reason: str,
    fallback_kind: str,
    seed: str,
    cycle: int,
    rank: int,
    item,
) -> CommentFallbackSelection:
    asset = item if isinstance(item, dict) else {}
    return CommentFallbackSelection(
        tenant_id=pool.tenant_id, task_id=pool.task_id,
        content_mix_contract_id=pool.content_mix_contract_id,
        target_ordinal=target_ordinal, assignment_version=1,
        selection_attempt=attempt, fallback_kind=fallback_kind,
        fallback_content_kind=content_kind, fallback_pool_snapshot_id=pool.id,
        selection_seed=seed, selection_cycle=cycle, selection_rank=rank,
        unicode_emoji=item if content_kind == "unicode_emoji" else None,
        material_id=asset.get("material_id"),
        asset_version_id=asset.get("asset_version_id"),
        asset_fingerprint=asset.get("asset_fingerprint"),
        tg_ref_version_id=asset.get("tg_ref_version_id"),
        tg_cache_peer_id=str(asset.get("tg_cache_peer_id") or ""),
        tg_cache_message_id=str(asset.get("tg_cache_message_id") or ""),
        asset_pool_hash=pool.image_meme_asset_pool_hash,
        fallback_reason=fallback_reason[:255], selection_state="ready",
    )


def _locked_cursor(
    session: Session,
    pool: ChannelCommentFallbackPoolSnapshot,
    content_kind: str,
    seed: str,
    ordered: list,
) -> FallbackShuffleBagCursor:
    cursor = session.scalar(
        select(FallbackShuffleBagCursor)
        .where(
            FallbackShuffleBagCursor.content_mix_contract_id
            == pool.content_mix_contract_id,
            FallbackShuffleBagCursor.fallback_content_kind == content_kind,
        )
        .with_for_update()
    )
    if cursor:
        return cursor
    cursor = FallbackShuffleBagCursor(
        tenant_id=pool.tenant_id,
        content_mix_contract_id=pool.content_mix_contract_id,
        fallback_content_kind=content_kind,
        bag_seed=seed,
        bag_order_hash=_stable_hash(ordered),
    )
    session.add(cursor)
    session.flush()
    return cursor


def _stable_bag(
    pool: ChannelCommentFallbackPoolSnapshot,
    policy: CommentFallbackPolicySnapshot,
    content_kind: str,
    items: list,
) -> tuple[list, str]:
    pool_hash = (
        policy.unicode_allowlist_hash
        if content_kind == "unicode_emoji"
        else pool.image_meme_asset_pool_hash
    )
    seed = _stable_hash([
        pool.task_id, pool.channel_message_id, pool.comment_plan_revision,
        content_kind, pool_hash,
    ])
    return sorted(items, key=lambda item: _stable_hash([seed, item])), seed


def _select_kind(
    policy: CommentFallbackPolicySnapshot,
    *,
    task_id: str,
    pool: ChannelCommentFallbackPoolSnapshot,
    target_ordinal: int,
) -> str:
    value = int(_stable_hash([
        task_id, pool.channel_message_id, pool.comment_plan_revision,
        target_ordinal, policy.fallback_policy_version,
    ])[:8], 16) % 10000
    if policy.unicode_enabled and value < policy.unicode_weight_bps:
        return "unicode_emoji"
    return "image_meme"


def _require_ready_material_group(session: Session, tenant_id: int, config: dict) -> None:
    group_id = config.get("image_meme_material_group_id")
    if not group_id:
        raise ValueError("image_meme_material_group_required")
    group = session.get(MaterialGroup, int(group_id))
    if group and group.membership_state == "review_required":
        raise ValueError("material_group_membership_review_required")
    if group and group.membership_state != "ready":
        raise ValueError("material_group_membership_invalid")
    if not ready_image_assets(session, tenant_id, int(group_id)):
        raise ValueError("image_meme_material_group_has_no_ready_assets")


def _latest_selection(
    session: Session,
    content_mix_contract_id: str,
    target_ordinal: int,
) -> CommentFallbackSelection | None:
    return session.scalar(
        select(CommentFallbackSelection)
        .where(
            CommentFallbackSelection.content_mix_contract_id == content_mix_contract_id,
            CommentFallbackSelection.target_ordinal == target_ordinal,
            CommentFallbackSelection.assignment_version == 1,
        )
        .order_by(CommentFallbackSelection.selection_attempt.desc())
        .limit(1)
    )


def _pool_snapshot(
    session: Session,
    content_mix_contract_id: str,
) -> ChannelCommentFallbackPoolSnapshot | None:
    return session.scalar(select(ChannelCommentFallbackPoolSnapshot).where(
        ChannelCommentFallbackPoolSnapshot.content_mix_contract_id
        == content_mix_contract_id,
    ))


def _gateway_started(session: Session, action_id: str) -> bool:
    return bool(session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == action_id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1)))


def _stable_hash(value) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["CommentFallbackUnavailable", "SelectedCommentFallback",
           "UNICODE_EMOJI_ALLOWLIST_V2", "freeze_comment_fallback_contract",
           "select_comment_fallback", "validate_comment_fallback_config"]
