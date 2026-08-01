from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, RemoteReconcileCase, TgAccount, TgGroup

from .remote_reconciliation import RemoteReconcileEvidence
from .runtime_state_hash import canonical_state_hash


HISTORY_LIMIT = 100
HISTORY_BEFORE_SECONDS = 120
HISTORY_AFTER_SECONDS = 600


def preview_remote_history_evidence(
    session: Session,
    case_id: str,
    *,
    gateway_client,
    credentials_resolver,
) -> RemoteReconcileEvidence:
    case, action, attempt = _case_facts(session, case_id)
    if not _text_group_send_supported(action):
        return _inconclusive(case, "telegram_history_unsupported_action")
    account = session.get(TgAccount, attempt.account_id)
    payload = action.payload if isinstance(action.payload, dict) else {}
    group = session.get(TgGroup, payload.get("group_id"))
    if not _identity_is_frozen(action, attempt, account, group):
        return _inconclusive(case, "telegram_history_identity_mismatch")
    try:
        snapshots = gateway_client.fetch_group_messages(
            account.id,
            group.tg_peer_id,
            account.session_ciphertext,
            credentials_resolver(session, account),
            limit=HISTORY_LIMIT,
        )
    except Exception as exc:  # noqa: BLE001 - error class is evidence, detail may leak.
        return _inconclusive(
            case,
            f"telegram_history_error_{type(exc).__name__}",
        )
    matches = _exact_matches(action, attempt, snapshots)
    fingerprint = _history_fingerprint(case, attempt, snapshots, matches)
    if len(matches) == 1:
        return RemoteReconcileEvidence(
            result="remote_confirmed",
            source="telegram_history_read_only",
            evidence_fingerprint=fingerprint,
            remote_message_id=str(matches[0].remote_message_id),
            remote_mutation_started=True,
            exact_match_count=1,
        )
    return RemoteReconcileEvidence(
        result="inconclusive",
        source="telegram_history_read_only",
        evidence_fingerprint=fingerprint,
        exact_match_count=len(matches),
    )


def _case_facts(
    session: Session,
    case_id: str,
) -> tuple[RemoteReconcileCase, Action, ExecutionAttempt]:
    case = session.get(RemoteReconcileCase, case_id)
    if case is None:
        raise ValueError("remote_reconcile_case_not_found")
    action = session.get(Action, case.action_id)
    attempt = session.get(ExecutionAttempt, case.execution_attempt_id)
    if action is None or attempt is None:
        raise RuntimeError("remote_reconcile_fact_incomplete")
    return case, action, attempt


def _text_group_send_supported(action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return bool(
        action.action_type == "send_message"
        and payload.get("group_id")
        and str(payload.get("message_text") or "")
        and not payload.get("media_segments")
        and not payload.get("source_media_asset_ids")
    )


def _identity_is_frozen(
    action: Action,
    attempt: ExecutionAttempt,
    account: TgAccount | None,
    group: TgGroup | None,
) -> bool:
    return bool(
        account is not None
        and group is not None
        and attempt.account_id == action.account_id == account.id
        and account.tenant_id == action.tenant_id == group.tenant_id
        and account.session_ciphertext
    )


def _exact_matches(action: Action, attempt: ExecutionAttempt, snapshots) -> list:
    payload = action.payload if isinstance(action.payload, dict) else {}
    expected = str(payload.get("message_text") or "")
    start, end = _history_window(attempt)
    return [
        row for row in snapshots
        if str(getattr(row, "content", "") or "") == expected
        and bool(getattr(row, "remote_message_id", ""))
        and bool(getattr(row, "viewer_peer_id", ""))
        and getattr(row, "viewer_peer_id", "")
        == getattr(row, "sender_peer_id", "")
        and _within_window(getattr(row, "sent_at", None), start, end)
    ]


def _history_window(attempt: ExecutionAttempt) -> tuple[datetime, datetime]:
    anchor = attempt.gateway_call_started_at or attempt.before_call_at
    if anchor is None:
        raise RuntimeError("remote_reconcile_gateway_time_missing")
    start = _as_utc(anchor) - timedelta(seconds=HISTORY_BEFORE_SECONDS)
    finish = attempt.after_call_at or anchor
    end = _as_utc(finish) + timedelta(seconds=HISTORY_AFTER_SECONDS)
    return start, end


def _within_window(
    value: datetime | None,
    start: datetime,
    end: datetime,
) -> bool:
    return bool(value is not None and start <= _as_utc(value) <= end)


def _as_utc(value: datetime) -> datetime:
    observed = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc)


def _history_fingerprint(case, attempt, snapshots, matches) -> str:
    start, end = _history_window(attempt)
    return canonical_state_hash({
        "case_id": case.id,
        "request_identity": (
            (attempt.result_snapshot or {}).get("gateway_request_identity") or ""
        ),
        "window_start": start,
        "window_end": end,
        "fetched_count": len(snapshots),
        "exact_remote_ids": sorted(
            str(row.remote_message_id) for row in matches
        ),
    })


def _inconclusive(
    case: RemoteReconcileCase,
    source: str,
) -> RemoteReconcileEvidence:
    return RemoteReconcileEvidence(
        result="inconclusive",
        source=source,
        evidence_fingerprint=canonical_state_hash({
            "case_id": case.id,
            "source": source,
        }),
    )


__all__ = ["preview_remote_history_evidence"]
