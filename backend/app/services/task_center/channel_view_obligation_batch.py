"""Freeze the complete view demand with batch identity and assignment reads."""
from sqlalchemy import select, tuple_

from app.models import Action, ViewFulfillmentObligation
from .account_assignment_eligibility import UNIFIED_CONTRACT, assignment_decisions
from .channel_obligation_lifecycle import release_obligation_action
from .engagement_runtime_error import RuntimeResourceBlocked


def ensure_view_obligations(session, task, *, ledger, actions):
    keys = tuple(dict.fromkeys((message.id, account_id) for message, account_id in actions))
    if not keys:
        return {}
    rows = list(session.scalars(select(ViewFulfillmentObligation).where(
        ViewFulfillmentObligation.task_day_ledger_id == ledger.id,
        tuple_(ViewFulfillmentObligation.channel_message_id,
            ViewFulfillmentObligation.account_id).in_(keys),
    )))
    owners = {(row.channel_message_id, row.account_id): row for row in rows}
    missing = tuple(key for key in keys if key not in owners)
    _require_assignments(session, task, missing)
    for message_id, account_id in missing:
        row = ViewFulfillmentObligation(tenant_id=ledger.tenant_id,
            task_day_ledger_id=ledger.id, channel_message_id=message_id, account_id=account_id)
        session.add(row)
        owners[(message_id, account_id)] = row
    session.flush()
    action_ids = {row.current_action_id for row in rows if row.current_action_id}
    bound = {row.id: row for row in session.scalars(select(Action).where(
        Action.id.in_(action_ids))) } if action_ids else {}
    for row in rows:
        release_obligation_action(row, bound.get(row.current_action_id))
    return owners


def _require_assignments(session, task, missing):
    if not missing or (task.type_config or {}).get("engagement_contract_version") != UNIFIED_CONTRACT:
        return
    reasons = assignment_decisions(session, task.tenant_id, (key[1] for key in missing))
    reason = next((reasons[account_id] for _, account_id in missing if reasons[account_id]), "")
    if reason:
        raise RuntimeResourceBlocked(reason, "账号无有效业务资格，不分配或调用新工作")
