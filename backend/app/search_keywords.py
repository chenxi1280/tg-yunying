from __future__ import annotations

import hashlib
import re

from app.security import decrypt_secret


KEYWORD_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def normalized_keyword_hash(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def strict_keyword_materials(hashes: list[str], ciphertexts: list[str]) -> list[tuple[str, str]]:
    normalized_hashes, normalized_ciphertexts = _normalized_material_values(hashes, ciphertexts)
    if len(normalized_hashes) != len(normalized_ciphertexts):
        raise ValueError("keyword_hashes 与 keyword_text_ciphertexts 必须一一对应")
    _validate_hashes(normalized_hashes)
    if len(set(normalized_hashes)) != len(normalized_hashes):
        raise ValueError("keyword_hashes 不允许重复")
    pairs = list(zip(normalized_hashes, normalized_ciphertexts, strict=True))
    for keyword_hash, ciphertext in pairs:
        _validate_keyword_material(keyword_hash, ciphertext)
    return pairs


def repair_legacy_keyword_materials(hashes: list[str], ciphertexts: list[str]) -> list[tuple[str, str]]:
    normalized_hashes, normalized_ciphertexts = _normalized_material_values(hashes, ciphertexts)
    if len(normalized_hashes) == len(normalized_ciphertexts):
        return strict_keyword_materials(normalized_hashes, normalized_ciphertexts)
    if len(normalized_ciphertexts) <= len(normalized_hashes):
        raise ValueError("keyword_hashes 与 keyword_text_ciphertexts 必须一一对应")
    _validate_hashes(normalized_hashes)
    if len(set(normalized_hashes)) != len(normalized_hashes):
        raise ValueError("keyword_hashes 不允许重复")
    ciphertext_by_hash = _ciphertexts_by_hash(normalized_ciphertexts, set(normalized_hashes))
    if set(ciphertext_by_hash) != set(normalized_hashes):
        raise ValueError("keyword_hashes 与 keyword_text_ciphertexts 的关键词内容不匹配")
    return [(keyword_hash, ciphertext_by_hash[keyword_hash]) for keyword_hash in normalized_hashes]


def _normalized_material_values(hashes: list[str], ciphertexts: list[str]) -> tuple[list[str], list[str]]:
    normalized_hashes = [str(item).strip().lower() for item in hashes if str(item).strip()]
    normalized_ciphertexts = [str(item).strip() for item in ciphertexts if str(item).strip()]
    return normalized_hashes, normalized_ciphertexts


def _validate_hashes(hashes: list[str]) -> None:
    if any(not KEYWORD_HASH_RE.fullmatch(item) for item in hashes):
        raise ValueError("keyword_hashes 必须是 64 位小写 hex")


def _ciphertexts_by_hash(ciphertexts: list[str], expected_hashes: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for ciphertext in ciphertexts:
        keyword_hash = _ciphertext_keyword_hash(ciphertext)
        if keyword_hash not in expected_hashes:
            raise ValueError("keyword_hashes 与 keyword_text_ciphertexts 的关键词内容不匹配")
        values.setdefault(keyword_hash, ciphertext)
    return values


def _validate_keyword_material(keyword_hash: str, ciphertext: str) -> None:
    if _ciphertext_keyword_hash(ciphertext) != keyword_hash:
        raise ValueError("keyword_hashes 与 keyword_text_ciphertexts 的关键词内容不匹配")


def _ciphertext_keyword_hash(ciphertext: str) -> str:
    try:
        keyword = decrypt_secret(ciphertext)
    except Exception as exc:
        raise ValueError("keyword_text_ciphertexts 包含无法解密内容") from exc
    if not (keyword or "").strip():
        raise ValueError("keyword_text_ciphertexts 不允许包含空关键词")
    return normalized_keyword_hash(keyword)
