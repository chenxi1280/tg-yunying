"""Exact, non-secret evidence for replacing never-called cancelled reaction work."""
import hashlib
import json
import re

from sqlalchemy import inspect, select

from app.models import (
    AccountBehaviorBudgetReservation, AccountPacingReservation, AccountPoolConcurrencyLease,
    Action, ExecutionAttempt, FulfillmentRemoteFact, GatewayRequestEvidenceJournal,
    ReactionFulfillmentObligation, ReactionRemoteFact, RemoteInvocationFence,
    SourcePacingAdmission, Task, TgAccount,
)
from app.services._common import _now
from app.timezone import as_beijing_aware


TASK_FIELDS = ("id", "tenant_id", "type", "status", "deleted_at", "retired_at",
    "task_lifecycle_epoch", "fulfillment_contract_version", "config_revision", "type_config",
    "account_config", "pacing_config", "failure_policy", "scheduled_start", "scheduled_end", "timezone")
EVIDENCE_MODELS = (ExecutionAttempt, GatewayRequestEvidenceJournal, FulfillmentRemoteFact,
    AccountBehaviorBudgetReservation, AccountPoolConcurrencyLease, RemoteInvocationFence,
    SourcePacingAdmission)


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def normalize_scope(spec):
    scope = {key: spec[key] for key in (
        "tenant_id", "task_ids", "action_ids", "expected_count", "deployed_sha")}
    if not isinstance(scope["tenant_id"], int) or scope["tenant_id"] <= 0:
        raise ValueError("reaction_backlog_tenant_invalid")
    for key in ("task_ids", "action_ids"):
        scope[key] = _identities(scope[key])
    if scope["expected_count"] != len(scope["action_ids"]):
        raise ValueError("reaction_backlog_count_mismatch")
    if re.fullmatch(r"[a-f0-9]{40}", str(scope["deployed_sha"])) is None:
        raise ValueError("reaction_backlog_sha_invalid")
    return scope


def _identities(values):
    if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
        raise ValueError("reaction_backlog_scope_invalid")
    if len(values) != len(set(values)):
        raise ValueError("reaction_backlog_duplicate_identity")
    return sorted(values)


def selected_rows(session, model, condition, *, lock=False):
    statement = select(model).where(condition).order_by(model.id)
    if lock:
        statement = statement.with_for_update(nowait=True)
    return list(session.scalars(statement.execution_options(populate_existing=True)))


def load_backlog_rows(session, scope, *, lock=False):
    tasks = selected_rows(session, Task, Task.id.in_(scope["task_ids"]), lock=lock)
    actions = selected_rows(session, Action, Action.id.in_(scope["action_ids"]))
    accounts = selected_rows(session, TgAccount,
        TgAccount.id.in_({row.account_id for row in actions}), lock=lock)
    if lock:
        actions = selected_rows(session, Action, Action.id.in_(scope["action_ids"]), lock=True)
    reservations = selected_rows(session, AccountPacingReservation,
        AccountPacingReservation.action_id.in_(scope["action_ids"]), lock=lock)
    obligations = selected_rows(session, ReactionFulfillmentObligation,
        ReactionFulfillmentObligation.current_action_id.in_(scope["action_ids"]), lock=lock)
    return tasks, actions, accounts, reservations, obligations


def preview_reaction_backlog(session, spec, *, lock=False):
    scope = normalize_scope(spec)
    rows = load_backlog_rows(session, scope, lock=lock)
    state = snapshot_rows(session, scope, rows)
    return {"scope": scope, "state": state, "state_hash": digest(state)}


def snapshot_rows(session, scope, rows):
    tasks, actions, accounts, reservations, obligations = rows
    _validate_scope_rows(scope, tasks, actions)
    task_map, account_map = {row.id: row for row in tasks}, {row.id: row for row in accounts}
    reservation_map = _owners(reservations, "action_id", len(actions))
    obligation_map = _owners(obligations, "current_action_id", len(actions))
    _reject_evidence(session, scope, obligations)
    items = []
    for action in actions:
        task, account = task_map.get(action.task_id), account_map.get(action.account_id)
        reservation, obligation = reservation_map[action.id], obligation_map[action.id]
        _validate_task(task, scope)
        _validate_action(action, task, account)
        _validate_bindings(action, reservation, obligation)
        items.append(_snapshot(action, reservation, obligation))
    task_states = [{"id": row.id, "hash": digest({key: getattr(row, key) for key in TASK_FIELDS})}
        for row in tasks]
    return {"scope": scope, "tasks": task_states, "actions": items}


def _validate_scope_rows(scope, tasks, actions):
    if {row.id for row in tasks} != set(scope["task_ids"]) or len(actions) != scope["expected_count"]:
        raise ValueError("reaction_backlog_count_mismatch")
    if {row.task_id for row in actions} != set(scope["task_ids"]):
        raise ValueError("reaction_backlog_task_scope_mismatch")


def _owners(rows, field, expected):
    result = {getattr(row, field): row for row in rows}
    if len(rows) != expected or len(result) != expected:
        raise ValueError("reaction_backlog_binding_count_mismatch")
    return result


def _validate_task(task, scope):
    if task is None or task.tenant_id != scope["tenant_id"] or task.type != "channel_like":
        raise ValueError("reaction_backlog_task_identity_mismatch")
    if task.status != "running" or task.deleted_at is not None or task.retired_at is not None:
        raise ValueError("reaction_backlog_task_not_running")
    if task.fulfillment_contract_version != "fact_first_v3":
        raise ValueError("reaction_backlog_contract_mismatch")


def _validate_action(action, task, account):
    identity = (action.tenant_id, action.task_type, action.action_type, action.task_lifecycle_epoch)
    expected = (task.tenant_id, "channel_like", "like_message", task.task_lifecycle_epoch)
    if identity != expected or account is None or account.tenant_id != task.tenant_id:
        raise ValueError("reaction_backlog_action_identity_mismatch")
    if action.status != "pending" or action.executed_at is not None:
        raise ValueError("reaction_backlog_action_state_invalid")
    if any((action.claim_owner, action.claim_token, action.lease_owner,
            action.lease_expires_at, action.claim_expires_at)):
        raise ValueError("reaction_backlog_action_owned")
    _validate_result(action.result or {})


def _validate_result(result):
    if (result.get("success") is True or result.get("remote_message_id")
            or result.get("gateway_call_started_at") or result.get("callback_mutation_started") is True
            or result.get("remote_mutation_started") not in (None, False)
            or "unknown" in str(result.get("error_code", ""))):
        raise ValueError("reaction_backlog_remote_evidence_present")


def _validate_bindings(action, reservation, obligation):
    identity = (reservation.state, reservation.tenant_id, reservation.task_id,
        reservation.account_id, reservation.pacing_slot_key)
    expected = ("cancelled", action.tenant_id, action.task_id, action.account_id, action.pacing_slot_key)
    if identity != expected:
        raise ValueError("reaction_backlog_reservation_mismatch")
    payload = action.payload or {}
    owner_identity = (obligation.status, obligation.tenant_id, obligation.task_id, obligation.account_id,
        obligation.id, obligation.channel_message_id, obligation.task_lifecycle_epoch)
    expected_owner = ("pending", action.tenant_id, action.task_id, action.account_id,
        payload.get("reaction_fulfillment_obligation_id"), payload.get("channel_message_id"), action.task_lifecycle_epoch)
    if owner_identity != expected_owner:
        raise ValueError("reaction_backlog_obligation_mismatch")


def _reject_evidence(session, scope, obligations):
    for model in EVIDENCE_MODELS:
        if session.scalar(select(model.action_id).where(
                model.action_id.in_(scope["action_ids"])).limit(1)) is not None:
            raise ValueError("reaction_backlog_remote_or_resource_evidence_present")
    if session.scalar(select(ReactionRemoteFact.id).where(ReactionRemoteFact.obligation_id.in_(
            [row.id for row in obligations])).limit(1)) is not None:
        raise ValueError("reaction_backlog_reaction_already_observed")


def _row_hash(row):
    return digest({column.key: getattr(row, column.key) for column in inspect(type(row)).columns})


def _snapshot(action, reservation, obligation):
    expired = reservation.source_deadline_at is not None and (
        as_beijing_aware(reservation.source_deadline_at) <= as_beijing_aware(_now()))
    return {"action_id": action.id, "task_id": action.task_id, "account_id": action.account_id,
        "action_version": action.action_version, "action_hash": _row_hash(action),
        "original_action_dedupe_key": action.action_dedupe_key,
        "reservation_id": reservation.id, "reservation_version": reservation.version,
        "reservation_hash": _row_hash(reservation), "timeline_hash": timeline_hash(reservation),
        "obligation_id": obligation.id,
        "obligation_hash": _row_hash(obligation), "source_expired": expired}


def timeline_hash(reservation):
    return digest({key: getattr(reservation, key) for key in (
        "tenant_id", "task_id", "account_id", "pacing_slot_key", "due_at",
        "release_not_before_at", "effective_claim_at", "source_deadline_at")})
