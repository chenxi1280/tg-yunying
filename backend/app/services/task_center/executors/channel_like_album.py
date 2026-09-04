"""Freeze album-level account participation before materializing child actions."""
import hashlib
import json

from sqlalchemy import select

from app.models import AlbumReactionParticipation, OperationTarget
from app.timezone import as_beijing

from ..album_reaction_facts import album_child_confirmed, album_child_unknown, album_extra_units
from ..album_reaction_timing import available_album_children
from ..channel_fulfillment import ensure_reaction_obligation
from ..daily_ledgers import ensure_task_day_ledger
from ..engagement_portfolio import reserve_portfolio_units
from ..pacing_quantity import deterministic_rank
from ..source_pacing import rolling_source_window
from .channel_like_capability import message_reaction_plan
from .channel_like_types import LikePlanItem


MAX_ALBUM_CHILDREN = 2


def logical_like_messages(messages):
    representatives = {}
    for message in sorted(messages, key=lambda m: m.message_id):
        key = ("album", message.grouped_id) if message.grouped_id else ("message", message.id)
        representatives.setdefault(key, message)
    return list(representatives.values())


def album_like_items(session, task, spec, representative):
    messages = [m for m in spec.messages if m.grouped_id == representative.grouped_id and (m.source_metadata or {}).get("photo")]
    if not messages:
        return []
    selected = _selected_ids(spec, representative)
    ledger = ensure_task_day_ledger(session, task, now=spec.now)
    channel = session.get(OperationTarget, representative.channel_target_id)
    base_units = sum(len(ids) for ids in (spec.allocated_ids_by_message or {}).values())
    extra_room = max(0, int(spec.config.get("daily_reaction_cap") or base_units) - base_units - album_extra_units(session, task, ledger))
    items = []
    parents = []
    for account_id in selected:
        parent = _parent(session, task, representative, account_id)
        if parent is None:
            parent = _freeze_parent(session, task, spec=spec, representative=representative,
                messages=messages, ledger=ledger, channel=channel, account_id=account_id, extra_room=extra_room)
            if parent is None:
                continue
            extra_room -= parent.child_count - 1
        parents.append(parent)
        items.extend(_pending_children(session, parent, messages, spec))
    return _child_ordinals(items, parents)


def _selected_ids(spec, representative):
    if spec.allocated_ids_by_message is not None:
        return list(spec.allocated_ids_by_message.get(representative.id, []))
    return [a.id for a in sorted(spec.accounts, key=lambda a: deterministic_rank(
        f"album:{representative.grouped_id}", str(a.id)))[:spec.target_per_message]]


def _parent(session, task, message, account_id):
    return session.scalar(select(AlbumReactionParticipation).where(
        AlbumReactionParticipation.task_id == task.id,
        AlbumReactionParticipation.lifecycle_epoch == task.task_lifecycle_epoch,
        AlbumReactionParticipation.channel_target_id == message.channel_target_id,
        AlbumReactionParticipation.album_id == message.grouped_id,
        AlbumReactionParticipation.account_id == account_id))


def _freeze_parent(session, task, *, spec, representative, messages, ledger, channel, account_id, extra_room):
    seed = f"{task.id}:{task.task_lifecycle_epoch}:{representative.grouped_id}:{account_id}"
    ranked = sorted(messages, key=lambda m: deterministic_rank(seed, str(m.message_id)))
    desired = 1 + int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) % MAX_ALBUM_CHILDREN
    count = min(desired, len(ranked), 1 + extra_room)
    reason = "stable_sample" if count == desired else "daily_capacity_or_child_count"
    _, deadline = rolling_source_window(task, representative.created_at)
    feasible = available_album_children(session, task, account_id=account_id,
        children=ranked[:count], due_at=spec.now, deadline_at=deadline)
    if feasible < count:
        count, reason = feasible, "timeline_capacity_single_child"
    if count == 0:
        task.last_error = "album_timeline_capacity_unavailable"
        return None
    planned = _plan_children(session, task, spec=spec, ranked=ranked[:count], seed=seed)
    if len(planned) != count:
        return None
    if count > 1:
        extra = reserve_portfolio_units(session, task, ledger, action_class="reaction",
            demand_identity=f"album-extra:{representative.grouped_id}:{account_id}",
            requested_units_by_account={account_id: 1})
        if not extra.achievable:
            count, reason = 1, "portfolio_capacity_single_child"
    children = _freeze_children(session, task, planned=planned[:count], account_id=account_id)
    parent = AlbumReactionParticipation(tenant_id=task.tenant_id, task_id=task.id,
        lifecycle_epoch=task.task_lifecycle_epoch, task_day_ledger_id=ledger.id,
        channel_target_id=representative.channel_target_id, target_peer_id=channel.tg_peer_id,
        album_id=representative.grouped_id, account_id=account_id,
        source_revision_hash=hashlib.sha256(json.dumps([(m.id, m.current_source_revision_id) for m in ranked], sort_keys=True).encode()).hexdigest(),
        children=children, child_count=count, child_count_reason=reason, deadline_at=deadline)
    session.add(parent)
    session.flush()
    return parent


def _plan_children(session, task, *, spec, ranked, seed):
    planned = []
    for message in ranked:
        reactions = message_reaction_plan(session, task, message, config=spec.config,
            reactions=spec.reactions, quantity=1, seed_id=f"{seed}:{message.id}")
        if not reactions:
            return []
        planned.append((message, reactions[0]))
    return planned


def _freeze_children(session, task, *, planned, account_id):
    children = []
    for message, reaction in planned:
        obligation = ensure_reaction_obligation(session, task, message, account_id)
        children.append({"message_id": message.id, "source_revision_id": message.current_source_revision_id,
                         "obligation_id": obligation.id, "reaction": reaction})
    return children


def _pending_children(session, parent, messages, spec):
    if parent.status == "confirmed":
        return []
    by_id = {m.id: m for m in messages}
    ready_ids = {a.id for a in spec.accounts}
    items = []
    for child in parent.children:
        if album_child_confirmed(session, parent, child):
            continue
        if album_child_unknown(session, child):
            parent.status = "unknown"
            continue
        message = by_id.get(child["message_id"])
        if message is None or message.current_source_revision_id != child["source_revision_id"]:
            parent.status = "source_child_changed_shortfall"
            continue
        if as_beijing(spec.now) >= as_beijing(parent.deadline_at):
            parent.status = "deadline_shortfall"
            continue
        if parent.account_id in ready_ids:
            items.append(LikePlanItem(message, parent.account_id, child["reaction"], 0, 0, child["obligation_id"]))
    return items


def _child_ordinals(items, parents):
    groups = {}
    for parent in parents:
        for child in parent.children:
            groups.setdefault(child["message_id"], []).append(parent.account_id)
    ranks = {message_id: {account_id: ordinal for ordinal, account_id in enumerate(sorted(ids))}
             for message_id, ids in groups.items()}
    return [LikePlanItem(item.message, item.account_id, item.reaction,
        ranks[item.message.id][item.account_id], len(ranks[item.message.id]), item.obligation_id) for item in items]
