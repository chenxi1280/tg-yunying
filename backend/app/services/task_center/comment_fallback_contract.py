from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


FALLBACK_POLICY_VERSION = "comment_fallback_v2"
UNICODE_ALLOWLIST_VERSION = "unicode_emoji_allowlist_v2"
MATERIAL_CONTRACT_VERSION = "material_library_v1"
UNICODE_EMOJI_ALLOWLIST_V2 = (
    "👍", "🙂", "👏", "🔥", "❤️", "😍", "🤩", "🎉", "💯", "🙌",
    "👌", "✨", "😄", "😊", "🥳", "👀", "🤝", "💪", "🌟", "💖",
)
UNICODE_ALLOWLIST_HASH = hashlib.sha256(
    json.dumps(UNICODE_EMOJI_ALLOWLIST_V2, ensure_ascii=False).encode("utf-8")
).hexdigest()


class CommentFallbackUnavailable(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SelectedCommentFallback:
    content_kind: str
    content: str
    media_segment: dict | None
    metadata: dict


__all__ = [
    "CommentFallbackUnavailable",
    "FALLBACK_POLICY_VERSION",
    "MATERIAL_CONTRACT_VERSION",
    "SelectedCommentFallback",
    "UNICODE_ALLOWLIST_HASH",
    "UNICODE_ALLOWLIST_VERSION",
    "UNICODE_EMOJI_ALLOWLIST_V2",
]
