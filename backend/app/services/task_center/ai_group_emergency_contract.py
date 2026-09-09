"""Identity and evidence checks shared by selection and the final send boundary."""
import hashlib
import json

from sqlalchemy import select

from app.models import (
    Action, ExecutionAttempt, FulfillmentRemoteFact, GatewayRequestEvidenceJournal,
    ProviderHttpExchange, ProviderHttpExchangeJob, SourcePacingAdmission,
)
from .comment_fallback_contract import UNICODE_ALLOWLIST_VERSION, UNICODE_EMOJI_ALLOWLIST_V2


POLICY_VERSION = "emergency_fallback_v1"
DIRECT_SOURCE = "emergency_check_in"
REPLY_SOURCE = "reply_unicode_emoji_fallback"
EMERGENCY_SOURCES = frozenset({DIRECT_SOURCE, REPLY_SOURCE})
UNICODE_POOL = UNICODE_EMOJI_ALLOWLIST_V2
UNICODE_POOL_VERSION = UNICODE_ALLOWLIST_VERSION
PENDING_STATUS = "emergency_pending"
UNKNOWN_STATUS = "provider_result_unknown"
IDENTITY_FIELDS = (
    "group_id", "chat_id", "primary_quantity_slot_id", "coverage_ledger_id", "daily_group_target_id",
    "target_operation_target_id", "reply_to_message_id", "conversation_turn_claim_id",
    "interaction_opportunity_id", "relation_kind", "act_type", "content_intent_id", "generation_job_id",
    "target_reference_revision", "operation_target_id",
)


def emergency_enabled(task) -> bool:
    config = dict(task.type_config or {})
    return bool(task.type == "group_ai_chat"
                and config.get("engagement_contract_version") == "unified_engagement_v1"
                and config.get("emergency_fallback_enabled", True))


def digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def selection_identity(action, payload) -> dict:
    from .payloads import SendMessagePayload

    payload = SendMessagePayload.model_validate(payload).model_dump(mode="json")
    return {"tenant_id": action.tenant_id, "task_id": action.task_id,
            "action_id": action.id, "account_id": action.account_id,
            "action_quantity_slot_id": action.primary_quantity_slot_id,
            "task_lifecycle_epoch": int(action.task_lifecycle_epoch or 1),
            "obligation_type": action.obligation_type, "obligation_id": action.obligation_id,
            **{key: payload.get(key) for key in IDENTITY_FIELDS}}


def selected_content(action_id: str, *, reply: bool) -> str:
    if not reply:
        return "签到"
    return UNICODE_POOL[int(digest(action_id), 16) % len(UNICODE_POOL)]


def is_response_payload(data) -> bool:
    return bool(data.get("reply_to_message_id") or data.get("conversation_turn_claim_id")
                or data.get("interaction_opportunity_id") or data.get("relation_kind") == "reply")


def telegram_unattempted(session, action) -> bool:
    from .ai_group_pre_gateway_discard import _uncalled

    siblings = tuple(session.scalars(select(Action).where(
        Action.primary_quantity_slot_id == action.primary_quantity_slot_id,
    ))) if action.primary_quantity_slot_id else (action,)
    if any(row.id != action.id and row.status in {"pending", "claiming", "executing", "retryable_failed"}
           for row in siblings):
        return False
    ids = tuple(row.id for row in siblings)
    attempts = tuple(session.scalars(select(ExecutionAttempt).where(ExecutionAttempt.action_id.in_(ids))))
    if any(not _uncalled(row) for row in attempts):
        return False
    if session.scalar(select(SourcePacingAdmission.id).where(
            SourcePacingAdmission.action_id.in_(ids), SourcePacingAdmission.state.in_(("call_started", "remote_unknown", "finished"))).limit(1)):
        return False
    if session.scalar(select(GatewayRequestEvidenceJournal.id).where(
            GatewayRequestEvidenceJournal.action_id.in_(ids)).limit(1)):
        return False
    return session.scalar(select(FulfillmentRemoteFact.fact_id).where(
        FulfillmentRemoteFact.action_id.in_(ids), FulfillmentRemoteFact.fact_kind != "safely_not_executed").limit(1)) is None


def unknown_content_evidence(session, action, job) -> dict | None:
    from .generation_provider_lineage import unresolved_exchange_statement

    unresolved_ids = set(session.scalars(unresolved_exchange_statement((job,))))
    rows = tuple(session.scalars(select(ProviderHttpExchange).join(
        ProviderHttpExchangeJob, ProviderHttpExchangeJob.exchange_id == ProviderHttpExchange.id,
    ).where(ProviderHttpExchangeJob.generation_job_id == job.id)))
    unknown = tuple(row for row in rows if row.outcome == "unknown")
    if not unknown or unresolved_ids != {row.id for row in unknown}:
        return None
    expected = (action.tenant_id, action.task_id, int(action.task_lifecycle_epoch or 1))
    if any((row.tenant_id, row.task_id, row.task_lifecycle_epoch) != expected
           or row.local_termination_confirmed is not True for row in unknown):
        return None
    return {"http_unknown_ids": sorted(row.id for row in unknown),
            "local_termination_confirmed": True, "provider_remote_outcome": "unknown"}
