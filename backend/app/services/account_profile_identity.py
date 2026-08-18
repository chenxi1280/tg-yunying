from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TgAccount, TgAccountProfileNameClaim
from app.services.account_profile_name_generation import generate_unique_display_names


SPACE_RE = re.compile(r"\s+")
ZERO_WIDTH_CHARACTERS = frozenset({"\u200b", "\u200c", "\u200d", "\ufeff"})
GENERIC_DISPLAY_NAMES = frozenset({"", "托管账号", "新托管账号", "未命名账号"})


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
