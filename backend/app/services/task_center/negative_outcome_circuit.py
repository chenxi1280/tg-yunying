from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.models import NegativeOutcomeCircuitState, NegativeOutcomePolicyRevision, Tenant
from app.services._common import _now
from app.timezone import as_beijing
from .engagement_policy_initialization import ensure_runtime_policy

EVENT_TYPES = frozenset({
    "bot_intercept", "admin_moderation", "user_retract", "ai_suspicion",
    "premature_answer", "unknown",
})
CIRCUIT_LEVELS = (
    "normal", "proactive_throttled", "response_restricted",
    "account_peer_quarantined", "manual_review",
)
AI_SUSPICION_KEYWORDS = frozenset({
    "你是ai", "你是机器人", "机器人", "假人", "robot", "bot", "人机", "机器号", "水军", "真人吗", "ai回复",
})


class NegativeOutcomeBlocked(Exception):
    def __init__(self, reason: str, details: str = "") -> None:
        super().__init__(f"{reason}: {details}" if details else reason)
        self.reason = reason
        self.details = details


def detect_ai_suspicion_in_text(text: str) -> bool:
    norm = str(text or "").lower().replace(" ", "")
    return any(kw in norm for kw in AI_SUSPICION_KEYWORDS)


def classify_negative_event(*, is_deleted=False, deleted_by_admin=False, content_text="", error_code=""):
    if deleted_by_admin or error_code == "user_banned_by_admin":
        return "admin_moderation"
    if error_code in {"tg_bot_detected_interception", "post_send_intercepted"}:
        return "bot_intercept"
    if detect_ai_suspicion_in_text(content_text):
        return "ai_suspicion"
    # A delete update does not identify who deleted the message or why.
    return "unknown"


def ensure_negative_outcome_policy(session, tenant_id):
    _lock_tenant(session, tenant_id)
    return ensure_runtime_policy(session, NegativeOutcomePolicyRevision,
        scope={"tenant_id": tenant_id}, defaults={"event_types": sorted(EVENT_TYPES)})


def _lock_tenant(session, tenant_id):
    # Only ingestion/initialization locks the tenant; normal gates are read-only.
    session.execute(select(Tenant.id).where(Tenant.id == tenant_id).with_for_update()).one()


def _circuit_query(*, tenant_id, peer_id, account_id, route):
    return select(NegativeOutcomeCircuitState).where(
        NegativeOutcomeCircuitState.tenant_id == tenant_id,
        NegativeOutcomeCircuitState.peer_id == peer_id,
        NegativeOutcomeCircuitState.account_id == account_id,
        NegativeOutcomeCircuitState.route == route,
    )


def get_or_create_circuit_state(session, *, tenant_id, peer_id, account_id=None, route=""):
    _lock_tenant(session, tenant_id)
    state = session.scalar(_circuit_query(
        tenant_id=tenant_id, peer_id=peer_id, account_id=account_id, route=route,
    ).with_for_update().execution_options(populate_existing=True))
    if state is None:
        state = NegativeOutcomeCircuitState(
            tenant_id=tenant_id, peer_id=peer_id, account_id=account_id, route=route,
        )
        session.add(state)
        session.flush()
    return state


def record_negative_outcome(
    session, *, tenant_id, peer_id, account_id=None, route="", event_type,
    event_id, evidence=None, observed_at=None,
):
    if not event_id:
        raise ValueError("negative_outcome_event_identity_required")
    if event_type not in EVENT_TYPES:
        raise ValueError("negative_outcome_event_type_invalid")
    policy = ensure_negative_outcome_policy(session, tenant_id)
    circuit = get_or_create_circuit_state(
        session, tenant_id=tenant_id, peer_id=peer_id, account_id=account_id, route=route,
    )
    current = as_beijing(_now())
    observed = as_beijing(observed_at or current)
    cutoff = current - timedelta(seconds=policy.recovery_window_seconds)
    if observed < cutoff or observed > current:
        return circuit
    if any(event.get("event_id") == event_id for event in circuit.events or []):
        return circuit
    events = _window_events(circuit, cutoff)
    events.append({"event_id": event_id, "event_type": event_type,
                   "recorded_at": observed.isoformat(), "evidence": evidence or {}})
    circuit.events = events
    circuit.policy_revision_id = policy.id
    count = sum(event["event_type"] in policy.event_types and event["event_type"] != "unknown"
                and not event.get("reviewed") for event in events)
    new_level = _level_for_count(policy, count)
    if CIRCUIT_LEVELS.index(new_level) > CIRCUIT_LEVELS.index(circuit.level):
        circuit.level = new_level
        circuit.entered_at = current
        circuit.reason = f"escalated_to_{new_level}_by_{event_type}"
    if event_type in policy.event_types and event_type != "unknown":
        circuit.eligible_exit_at = current + timedelta(seconds=policy.minimum_hold_seconds)
    circuit.version += 1
    circuit.updated_at = current
    session.flush()
    return circuit


def _window_events(circuit, cutoff):
    return [event for event in circuit.events or []
            if as_beijing(datetime.fromisoformat(event["recorded_at"])) >= cutoff]


def _level_for_count(policy, count):
    thresholds = (
        (policy.manual_review_threshold, "manual_review"),
        (policy.quarantine_threshold, "account_peer_quarantined"),
        (policy.response_restricted_threshold, "response_restricted"),
        (policy.proactive_throttled_threshold, "proactive_throttled"),
    )
    return next((level for threshold, level in thresholds if count >= threshold), "normal")


def evaluate_circuit_state(session, *, tenant_id, peer_id, account_id=None, route=""):
    # Recovery requires independent visibility evidence, not elapsed time alone.
    return session.scalar(_circuit_query(
        tenant_id=tenant_id, peer_id=peer_id, account_id=account_id, route=route,
    ).execution_options(populate_existing=True))


def recover_circuit_from_visibility(session, *, tenant_id, peer_id, account_id, route, observed_at):
    query = _circuit_query(tenant_id=tenant_id, peer_id=peer_id, account_id=account_id, route=route)
    current_state = session.scalar(query.execution_options(populate_existing=True))
    if current_state is None or current_state.level in {"normal", "manual_review"}:
        return
    _lock_tenant(session, tenant_id)
    circuit = session.scalar(query.with_for_update().execution_options(populate_existing=True))
    if circuit is None or circuit.level in {"normal", "manual_review"}:
        return
    current = as_beijing(observed_at)
    if not circuit.eligible_exit_at or current < as_beijing(circuit.eligible_exit_at):
        return
    policy = session.get(NegativeOutcomePolicyRevision, circuit.policy_revision_id) if circuit.policy_revision_id else None
    if policy is None:
        policy = ensure_negative_outcome_policy(session, tenant_id)
        circuit.policy_revision_id = policy.id
    remaining = _window_events(circuit, current - timedelta(seconds=policy.recovery_window_seconds))
    if any(event["event_type"] != "unknown" and not event.get("reviewed") for event in remaining):
        return
    circuit.events = remaining
    circuit.level = "normal"
    circuit.eligible_exit_at = None
    circuit.reason = "recovered_by_visible_message"
    circuit.version += 1
    circuit.updated_at = current
    session.flush()


def assert_negative_outcome_circuit_clear(
    session, *, tenant_id, peer_id, account_id=None, route="", action_kind="response",
):
    scopes = (None, account_id) if account_id is not None else (None,)
    for account_scope in scopes:
        circuit = evaluate_circuit_state(
            session, tenant_id=tenant_id, peer_id=peer_id, account_id=account_scope, route=route,
        )
        if circuit is not None:
            _check_level(circuit.level, action_kind)


def _check_level(level, action_kind):
    blocked = level in {"account_peer_quarantined", "manual_review"}
    blocked |= level == "response_restricted" and action_kind in {"response", "proactive"}
    blocked |= level == "proactive_throttled" and action_kind == "proactive"
    if blocked:
        raise NegativeOutcomeBlocked("negative_outcome_policy_blocked", f"circuit_in_{level}")
