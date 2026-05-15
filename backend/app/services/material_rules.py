from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Material


@dataclass
class MaterialRuleResult:
    selected: Material | None = None
    segment: dict[str, Any] | None = None
    action: str = "none"
    fallback: str = "text_only"
    failure_reason: str = ""
    candidate_count: int = 0

    @property
    def ok(self) -> bool:
        return self.selected is not None and self.segment is not None


def select_material_for_policy(
    session: Session,
    tenant_id: int,
    policy: dict[str, Any] | None,
    *,
    context_key: str = "",
    default_caption: str = "",
) -> MaterialRuleResult:
    policy = policy or {}
    if not policy or not bool(policy.get("enabled", True)):
        return MaterialRuleResult()
    fallback = str(policy.get("fallback") or "text_only")
    action = str(policy.get("action") or policy.get("mode_action") or "append_media")
    material = _select_ready_material(session, tenant_id, policy, context_key=context_key)
    candidates = _ready_material_candidates(session, tenant_id, policy)
    if not material:
        return MaterialRuleResult(action=action, fallback=fallback, failure_reason="cache_not_ready", candidate_count=len(candidates))
    caption = str(policy.get("caption") if policy.get("caption") is not None else default_caption)
    return MaterialRuleResult(
        selected=material,
        segment=material_to_segment(material, caption=caption),
        action=action,
        fallback=fallback,
        candidate_count=len(candidates),
    )


def material_to_segment(material: Material, *, caption: str = "") -> dict[str, Any]:
    source = material.content if material.emoji_asset_kind == "custom_emoji" else f"tg-cache://{material.tg_cache_peer_id}/{material.tg_cache_message_id}"
    return {
        "segment_type": material.material_type,
        "type": material.material_type,
        "source": source,
        "caption": caption,
        "material_id": material.id,
        "asset_version_id": material.asset_version_id,
        "tg_ref_version_id": material.tg_ref_version_id,
        "asset_fingerprint": material.asset_fingerprint,
        "emoji_asset_kind": material.emoji_asset_kind,
        "delivery_mode": material.delivery_mode,
    }


def _select_ready_material(session: Session, tenant_id: int, policy: dict[str, Any], *, context_key: str) -> Material | None:
    candidates = _ready_material_candidates(session, tenant_id, policy)
    if not candidates:
        return None
    mode = str(policy.get("mode") or "tag_match")
    if mode == "latest":
        return candidates[0]
    index_source = context_key or "|".join(str(policy.get(key) or "") for key in ("material_id", "material_ids", "required_tags", "material_type"))
    digest = hashlib.sha256(index_source.encode("utf-8")).hexdigest()
    return candidates[int(digest[:8], 16) % len(candidates)]


def _ready_material_candidates(session: Session, tenant_id: int, policy: dict[str, Any]) -> list[Material]:
    material_ids = _int_list(policy.get("material_ids"))
    if policy.get("material_id"):
        material_ids = [int(policy["material_id"])]
    stmt = select(Material).where(
        Material.tenant_id == tenant_id,
        Material.review_status == "已审核",
        Material.cache_ready_status == "ready",
    )
    material_type = str(policy.get("material_type") or "").strip()
    if material_type:
        stmt = stmt.where(Material.material_type == material_type)
    else:
        stmt = stmt.where(Material.material_type.in_(["图片", "表情包", "文件"]))
    if material_ids:
        stmt = stmt.where(Material.id.in_(material_ids))
    candidates = list(session.scalars(stmt.order_by(Material.id.desc()).limit(100)))
    required_tags = _str_list(policy.get("required_tags") or policy.get("tags"))
    if required_tags:
        candidates = [item for item in candidates if _matches_tags(item.tags, required_tags)]
    emoji_kinds = set(_str_list(policy.get("emoji_asset_kinds") or policy.get("emoji_asset_kind")))
    if emoji_kinds:
        candidates = [item for item in candidates if (item.emoji_asset_kind or "image_meme") in emoji_kinds]
    return [item for item in candidates if _sendable_candidate(item)]


def _sendable_candidate(material: Material) -> bool:
    if material.material_type == "表情包" and material.emoji_asset_kind == "custom_emoji":
        return material.content.startswith("custom_emoji:")
    return bool(material.tg_cache_peer_id and material.tg_cache_message_id)


def _matches_tags(raw_tags: str, required_tags: list[str]) -> bool:
    tags = {item.strip().lower() for item in raw_tags.replace("，", ",").split(",") if item.strip()}
    return all(tag.lower() in tags for tag in required_tags)


def _int_list(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, str):
        raw = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    result: list[int] = []
    for item in raw:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return result


def _str_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        raw = value.replace("，", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    return [str(item).strip() for item in raw if str(item).strip()]


__all__ = ["MaterialRuleResult", "material_to_segment", "select_material_for_policy"]
