"""Deterministic rejection of sexual-service advertisements, without rewriting."""

import hashlib
import re
import unicodedata


SCREENING_VERSION = "sexual_commerce_v1"
CONTENT_BLOCK_REASON = "sexual_commerce_advertisement"
BLOCKED_CONTENT_LABEL = "[已屏蔽性交易广告]"
SERVICE_PATTERN = re.compile(
    r"楼凤|樓鳳|援交|招嫖|性服务|性服務|外围女|外圍女|"
    r"上门做爱|上門做愛|陪睡|口交|肛交|无套|無套|"
    r"sex(?:ual)?services?|escortservices?|paidsex|sexualmassage",
    re.IGNORECASE,
)
TRANSACTION_PATTERN = re.compile(
    r"预约|預約|预订|預訂|收费|收費|价目|價目|价格[:：]|價格[:：]|"
    r"客服|联系方式|聯繫方式|联系[:：]|聯繫[:：]|"
    r"\d{2,}(?:元|米|rmb|usd)|[$¥￥]\d+|"
    r"https?://|t\.me/|@[a-z0-9_]{3,}|"
    r"book(?:now|ing)|rates?[:：$]|call[:：+]|contact[:：@]",
    re.IGNORECASE,
)


def normalized_screening_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return "".join(char for char in text if not char.isspace() and unicodedata.category(char) != "Cf")


def content_screening_reason(value: str) -> str:
    text = normalized_screening_text(value)
    if SERVICE_PATTERN.search(text) and TRANSACTION_PATTERN.search(text):
        return CONTENT_BLOCK_REASON
    return ""


def screened_public_text(value: str) -> str:
    return BLOCKED_CONTENT_LABEL if content_screening_reason(value) else value


def content_fingerprint(value: str) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()


def outbound_segments_blocked(content: str, segments=()) -> bool:
    values = [content or ""]
    for segment in segments or ():
        values.extend((getattr(segment, "content", "") or "", getattr(segment, "caption", "") or ""))
    return bool(content_screening_reason("\n".join(values)))
