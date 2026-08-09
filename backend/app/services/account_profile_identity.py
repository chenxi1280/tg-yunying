from __future__ import annotations

import random
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TgAccount, TgAccountProfileNameClaim


SPACE_RE = re.compile(r"\s+")
ZERO_WIDTH_CHARACTERS = frozenset({"\u200b", "\u200c", "\u200d", "\ufeff"})
NAME_GENERATION_MAX_ATTEMPTS = 10_000
GENERIC_DISPLAY_NAMES = frozenset({"", "托管账号", "新托管账号", "未命名账号"})

NAME_PREFIXES = ("薄荷", "海盐", "青柠", "云朵", "晚风", "橘子", "山茶", "木槿", "星河", "松露", "青团", "麦芽", "栗子", "小葵", "南星", "半夏")
NAME_OBJECTS = ("日记", "信箱", "书签", "汽水", "糖罐", "窗台", "耳机", "胶片", "风铃", "茶杯", "纸飞机", "便利店", "小卖部", "备忘录", "收音机", "口袋")
NAME_ACTIONS = ("散步中", "等风来", "看晚霞", "听小雨", "晒太阳", "慢慢走", "在发呆", "先收藏", "不熬夜", "去兜风", "等天晴", "喝热茶", "翻一页", "看云去", "吹晚风", "捡星光")
NAME_MOODS = ("慢半拍", "有点甜", "刚刚好", "很松弛", "不着急", "轻轻的", "小透明", "微微困", "心情晴", "今天闲", "偶尔冒泡", "随便看看")
NAME_SCENES = ("凌晨路灯", "周末阳台", "雨后街角", "黄昏车站", "夏夜窗口", "清晨厨房", "海边长椅", "楼下花店", "巷口咖啡", "树下长椅", "午后书店", "夜班便利店")
NAME_SHORTS = ("阿柚", "小葵", "山风", "七七", "橘白", "南星", "一栗", "鹿鹿", "木子", "小满", "青团", "半夏", "小禾", "初九", "一朵", "麦麦")


class DisplayNameConflict(ValueError):
    pass


@dataclass(frozen=True)
class NameClaimRequest:
    tenant_id: int
    account_id: int
    display_name: str
    source: str
    actor: str
    trace_id: str = ""
    batch_id: int | None = None
    batch_item_id: int | None = None


@dataclass(frozen=True)
class DuplicateNameGroup:
    name_key: str
    keeper_account_id: int
    target_account_ids: tuple[int, ...]


def normalize_display_name(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    visible = "".join(char for char in normalized if char not in ZERO_WIDTH_CHARACTERS)
    return SPACE_RE.sub(" ", visible).strip().casefold()


def unavailable_name_keys(session: Session, tenant_id: int) -> set[str]:
    account_names = session.scalars(
        select(TgAccount.display_name).where(TgAccount.tenant_id == tenant_id, TgAccount.deleted_at.is_(None))
    )
    claim_keys = session.scalars(
        select(TgAccountProfileNameClaim.name_key).where(TgAccountProfileNameClaim.tenant_id == tenant_id)
    )
    return {key for value in account_names if (key := normalize_display_name(value))} | set(claim_keys)


def claim_profile_names(session: Session, requests: Sequence[NameClaimRequest]) -> list[TgAccountProfileNameClaim]:
    _reject_request_conflicts(requests)
    existing, new_requests = _partition_existing_claims(session, requests)
    claims = [_claim_from_request(request) for request in new_requests]
    try:
        with session.begin_nested():
            session.add_all(claims)
            session.flush()
    except IntegrityError as exc:
        raise DisplayNameConflict("display_name_conflict") from exc
    return existing + claims


def _partition_existing_claims(
    session: Session,
    requests: Sequence[NameClaimRequest],
) -> tuple[list[TgAccountProfileNameClaim], list[NameClaimRequest]]:
    existing: list[TgAccountProfileNameClaim] = []
    new_requests: list[NameClaimRequest] = []
    for request in requests:
        claim = session.scalar(
            select(TgAccountProfileNameClaim).where(
                TgAccountProfileNameClaim.tenant_id == request.tenant_id,
                TgAccountProfileNameClaim.name_key == normalize_display_name(request.display_name),
            )
        )
        if claim is None:
            new_requests.append(request)
        elif claim.account_id == request.account_id:
            existing.append(claim)
        else:
            raise DisplayNameConflict("display_name_conflict")
    return existing, new_requests


def _claim_from_request(request: NameClaimRequest) -> TgAccountProfileNameClaim:
    name_key = normalize_display_name(request.display_name)
    if not name_key or request.display_name.strip() in GENERIC_DISPLAY_NAMES:
        raise DisplayNameConflict("display_name_invalid")
    return TgAccountProfileNameClaim(
        tenant_id=request.tenant_id,
        account_id=request.account_id,
        display_name=request.display_name.strip(),
        name_key=name_key,
        source=request.source,
        batch_id=request.batch_id,
        batch_item_id=request.batch_item_id,
        trace_id=request.trace_id,
        created_by=request.actor,
    )


def _reject_request_conflicts(requests: Sequence[NameClaimRequest]) -> None:
    keys = [normalize_display_name(request.display_name) for request in requests]
    if len(keys) != len(set(keys)):
        raise DisplayNameConflict("display_name_conflict")


def assert_profile_name_claimed(session: Session, tenant_id: int, account_id: int, display_name: str) -> None:
    claim = session.scalar(
        select(TgAccountProfileNameClaim).where(
            TgAccountProfileNameClaim.tenant_id == tenant_id,
            TgAccountProfileNameClaim.name_key == normalize_display_name(display_name),
        )
    )
    if claim is None or claim.account_id != account_id:
        raise DisplayNameConflict("display_name_claim_missing")


def generate_unique_display_names(
    count: int,
    unavailable_keys: set[str],
    seed: str,
    *,
    forbidden_words: set[str] | None = None,
) -> list[str]:
    if count < 0:
        raise ValueError("count must not be negative")
    generator = random.Random(seed)
    template_offset = generator.randrange(9)
    names: list[str] = []
    used = set(unavailable_keys)
    forbidden = {word.strip() for word in (forbidden_words or set()) if word.strip()}
    for attempt in range(NAME_GENERATION_MAX_ATTEMPTS):
        candidate = _random_display_name(generator, attempt + template_offset)
        key = normalize_display_name(candidate)
        if key in used or any(word in candidate for word in forbidden):
            continue
        names.append(candidate)
        used.add(key)
        if len(names) == count:
            return names
    raise RuntimeError("name_pool_exhausted")


def _random_display_name(generator: random.Random, slot: int) -> str:
    templates = (
        lambda: generator.choice(NAME_SHORTS),
        lambda: f"{generator.choice(NAME_PREFIXES)}{generator.choice(NAME_OBJECTS)}",
        lambda: f"{generator.choice(NAME_PREFIXES)}{generator.choice(NAME_ACTIONS)}",
        lambda: f"{generator.choice(NAME_SCENES)}{generator.choice(NAME_MOODS)}",
        lambda: f"{generator.choice(NAME_OBJECTS)}旁边{generator.choice(NAME_ACTIONS)}",
        lambda: f"{generator.choice(NAME_PREFIXES)}今天{generator.choice(NAME_MOODS)}",
        lambda: f"在{generator.choice(NAME_SCENES)}{generator.choice(NAME_ACTIONS)}",
        lambda: f"{generator.choice(NAME_MOODS)}的{generator.choice(NAME_OBJECTS)}",
        lambda: f"{generator.choice(NAME_PREFIXES)}和{generator.choice(NAME_OBJECTS)}",
    )
    return templates[slot % len(templates)]()


def duplicate_name_groups(accounts: Iterable[TgAccount]) -> list[DuplicateNameGroup]:
    grouped: dict[str, list[TgAccount]] = {}
    for account in accounts:
        key = normalize_display_name(account.display_name)
        if key:
            grouped.setdefault(key, []).append(account)
    return [_duplicate_group(key, rows) for key, rows in sorted(grouped.items()) if len(rows) > 1]


def _duplicate_group(name_key: str, accounts: list[TgAccount]) -> DuplicateNameGroup:
    ordered = sorted(accounts, key=_keeper_sort_key)
    return DuplicateNameGroup(
        name_key=name_key,
        keeper_account_id=ordered[0].id,
        target_account_ids=tuple(account.id for account in ordered[1:]),
    )


def _keeper_sort_key(account: TgAccount) -> tuple[int, int, datetime, int]:
    created_at = account.created_at or datetime.max
    return (
        0 if account.profile_sync_status == "已同步" else 1,
        0 if account.avatar_object_key else 1,
        created_at,
        account.id,
    )


__all__ = [
    "DisplayNameConflict",
    "DuplicateNameGroup",
    "NameClaimRequest",
    "assert_profile_name_claimed",
    "claim_profile_names",
    "duplicate_name_groups",
    "generate_unique_display_names",
    "normalize_display_name",
    "unavailable_name_keys",
]
