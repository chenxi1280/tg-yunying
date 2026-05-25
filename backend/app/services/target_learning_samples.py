from __future__ import annotations

from app.models import ChannelMessageComment, TargetLearningSample


def refresh_comment_sample(
    sample: TargetLearningSample,
    comment: ChannelMessageComment,
    status: str,
    reject_reason: str,
    downweight_reason: str,
    quality_score: int,
) -> None:
    sample.sender_peer_id = comment.author_peer_id or ""
    sample.sender_username = str(getattr(comment, "author_username", "") or "").lstrip("@")
    sample.sender_name = comment.author_name or ""
    sample.is_bot = bool(getattr(comment, "is_bot", False))
    sample.is_managed_account = status == "rejected" and reject_reason == "managed_account"
    sample.text = (comment.content_preview or "")[:4000]
    sample.learning_status = status
    sample.reject_reason = reject_reason
    sample.downweight_reason = downweight_reason
    sample.quality_score = quality_score
    sample.observed_reply_count = int(comment.reply_count or 0)
    sample.sent_at = comment.published_at
    if status != "accepted":
        sample.applied_profile_version = None
