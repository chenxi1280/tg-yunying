from __future__ import annotations

from app.services._common import _now


def advance_full_initialization(owner) -> None:
    owner.failure_type = ""
    owner.failure_detail = ""
    owner.finished_at = None
    owner.lease_token = ""
    owner.lease_expires_at = None
    if not _profile_succeeded(owner):
        _set_pending(owner, "profile", None)
    elif _abc_requires_preparation(owner):
        _set_pending(owner, "abc", None)
    elif not _two_fa_succeeded(owner):
        _set_pending(owner, "two_fa", owner.two_fa_next_retry_at)
    elif not _abc_succeeded(owner):
        _set_pending(owner, "abc", None)
    else:
        _finish(owner)
    owner.version += 1


def _set_pending(owner, stage: str, next_retry_at) -> None:
    owner.status = "pending"
    owner.stage = stage
    owner.next_retry_at = next_retry_at


def _finish(owner) -> None:
    owner.status = "succeeded"
    owner.stage = "succeeded"
    owner.next_retry_at = None
    owner.finished_at = _now()


def _profile_succeeded(owner) -> bool:
    return owner.profile_status == "succeeded" and bool(owner.profile_evidence_ref)


def _two_fa_succeeded(owner) -> bool:
    return owner.two_fa_status == "succeeded" and bool(owner.two_fa_evidence_ref)


def _abc_succeeded(owner) -> bool:
    return owner.abc_status == "succeeded" and bool(owner.abc_evidence_ref)


def _abc_requires_preparation(owner) -> bool:
    return not _abc_succeeded(owner) and owner.abc_status != "waiting_prerequisite"


__all__ = ["advance_full_initialization"]
