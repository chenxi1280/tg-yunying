from __future__ import annotations


PROFILE_SYNCED_STATUS = "已同步"


def comment_account_profile_ready(account) -> bool:
    return all(
        [
            _has_chinese_text(account.tg_first_name),
            bool(str(account.username or "").strip()),
            bool(str(account.avatar_object_key or "").strip()),
            str(account.profile_sync_status or "").strip()
            == PROFILE_SYNCED_STATUS,
        ]
    )


def config_with_comment_profile(
    config: dict,
    profile_preview: dict,
) -> dict:
    summary = str(profile_preview.get("profile_hit_summary") or "").strip()
    if not summary:
        return dict(config)
    return {**config, "target_comment_profile": summary}


def _has_chinese_text(value: str | None) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(value or ""))


__all__ = ["comment_account_profile_ready", "config_with_comment_profile"]
