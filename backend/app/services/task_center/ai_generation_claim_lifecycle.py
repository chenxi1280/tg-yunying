from __future__ import annotations

from sqlalchemy import select

from app.models import Action
from app.services._common import _now

from .ai_generation_timing import GENERATION_LEASE
from .ai_quality_stats import record_provider_admission_unavailable


def mark_generation_claim(action: Action, owner: str, token: str) -> None:
    payload = dict(action.payload) if isinstance(action.payload, dict) else {}
    payload["ai_generation_status"] = "generating"
    payload["ai_generation_claim_owner"] = owner
    payload["ai_generation_claim_token"] = token
    action.payload = payload
    action.status = "executing"
    action.claim_owner = owner
    action.claim_token = token
    action.lease_owner = owner
    action.lease_expires_at = _now() + GENERATION_LEASE


def owns_generation_claim(action: Action | None, owner: str, token: str) -> bool:
    return bool(
        action
        and action.status == "executing"
        and action.claim_owner == owner
        and action.claim_token == token
    )


def release_prepared_batch(session_factory, owner: str, token: str) -> int:
    with session_factory() as session:
        actions = list(session.scalars(select(Action).where(
            Action.status == "executing",
            Action.claim_owner == owner,
            Action.claim_token == token,
        )))
        for action in actions:
            payload = action.payload if isinstance(action.payload, dict) else {}
            if not str(payload.get("message_text") or "").strip():
                raise RuntimeError(f"AI generation action {action.id} completed without content")
            release_generation_claim(action, payload)
        session.commit()
        return len(actions)


def release_unprepared_batch(
    session_factory,
    owner: str,
    token: str,
    *,
    provider_admission_unavailable: bool = False,
) -> None:
    with session_factory() as session:
        actions = list(session.scalars(select(Action).where(
            Action.status == "executing",
            Action.claim_owner == owner,
            Action.claim_token == token,
        )))
        for action in actions:
            if provider_admission_unavailable:
                record_provider_admission_unavailable(session, action)
            release_generation_claim(action, dict(action.payload or {}))
        session.commit()


def persisted_generation_failure(session_factory, action_id: str) -> bool:
    with session_factory() as session:
        action = session.get(Action, action_id)
        result = action.result if action and isinstance(action.result, dict) else {}
        persisted = bool(
            action
            and str(result.get("error_code") or "")
            and (
                action.status in {"failed", "skipped"}
                or (
                    action.status == "pending"
                    and result.get("error_code") == "context_freshness_unproven"
                )
            )
        )
        if persisted and action.status in {"failed", "skipped"}:
            _release_failed_action_reservations(session, action)
        return persisted


def release_generation_claim(action: Action, payload: dict) -> None:
    payload = dict(payload)
    if (
        payload.get("ai_generation_status") == "generating"
        and not str(payload.get("message_text") or "").strip()
    ):
        payload["ai_generation_status"] = "pending"
    payload["ai_generation_claim_owner"] = ""
    payload["ai_generation_claim_token"] = ""
    action.payload = payload
    action.status = "pending"
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.lease_owner = ""
    action.lease_expires_at = None
    action.executed_at = None


def _release_failed_action_reservations(session, action: Action) -> None:  # noqa: ANN001
    from . import dispatcher
    from .conversation_speaker_rotation import release_group_ai_speaker_reservation

    release_group_ai_speaker_reservation(session, action)
    dispatcher._sync_action_content_mix_state(session, action)
    session.commit()


__all__ = [
    "mark_generation_claim",
    "owns_generation_claim",
    "persisted_generation_failure",
    "release_prepared_batch",
    "release_unprepared_batch",
]
