from __future__ import annotations

from app.models import CommentFallbackSelection

from .comment_fallback_contract import SelectedCommentFallback


def selection_result(row: CommentFallbackSelection) -> SelectedCommentFallback:
    metadata = {
        "selection_id": row.id,
        "fallback_kind": row.fallback_kind,
        "fallback_content_kind": row.fallback_content_kind,
        "selection_cycle": row.selection_cycle,
        "selection_rank": row.selection_rank,
        "selection_attempt": row.selection_attempt,
        "asset_pool_hash": row.asset_pool_hash,
        "material_id": row.material_id,
        "asset_version_id": row.asset_version_id,
        "asset_fingerprint": row.asset_fingerprint,
        "tg_ref_version_id": row.tg_ref_version_id,
        "tg_cache_peer_id": row.tg_cache_peer_id,
        "tg_cache_message_id": row.tg_cache_message_id,
    }
    if row.fallback_content_kind == "unicode_emoji":
        return SelectedCommentFallback(
            "unicode_emoji", row.unicode_emoji or "", None, metadata,
        )
    segment = {
        "segment_type": "表情包", "type": "表情包",
        "source": f"tg-cache://{row.tg_cache_peer_id}/{row.tg_cache_message_id}",
        "caption": "", "material_id": row.material_id,
        "asset_version_id": row.asset_version_id,
        "tg_ref_version_id": row.tg_ref_version_id,
        "asset_fingerprint": row.asset_fingerprint,
        "emoji_asset_kind": "image_meme", "delivery_mode": "download_reupload",
    }
    return SelectedCommentFallback("image_meme", "", segment, metadata)


__all__ = ["selection_result"]
