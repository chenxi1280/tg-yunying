"""Present an album once, with account completion distinct from child RPC counts."""
from collections import defaultdict

from sqlalchemy import select

from app.models import AlbumReactionParticipation, ChannelMessage
from .album_reaction_facts import album_child_confirmed, album_child_unknown, configured_album_accounts


def album_progress(session, task):
    parents = session.scalars(select(AlbumReactionParticipation).where(
        AlbumReactionParticipation.task_id == task.id,
        AlbumReactionParticipation.lifecycle_epoch == task.task_lifecycle_epoch))
    by_album = defaultdict(list)
    for parent in parents:
        by_album[parent.album_id].append(parent)
    targets = configured_album_accounts(session, task)
    return {album: _progress(rows, targets.get(album), session=session) for album, rows in by_album.items()}


def _progress(parents, target, *, session):
    confirmed = [sum(album_child_confirmed(session, p, c) for c in p.children) for p in parents]
    finished = sum(n == p.child_count for p, n in zip(parents, confirmed))
    unknown = sum(any(album_child_unknown(session, c) for c in p.children) for p in parents)
    failed = sum(p.status in {"source_child_changed_shortfall", "deadline_shortfall"} for p in parents)
    return {"target_count": target, "completed_count": finished,
        "materialized_accounts": len(parents), "planned_child_rpc": sum(p.child_count for p in parents),
        "confirmed_child_reactions": sum(confirmed), "unknown_accounts": unknown,
        "failed_count": failed, "running_count": max(0, len(parents)-finished-unknown-failed),
        "capacity_shortfall": max(0, (target or 0)-len(parents))+failed}


def merge_album_message_groups(session, task, groups):
    if task.type != "channel_like" or (task.type_config or {}).get("engagement_contract_version") != "unified_engagement_v1":
        return groups
    progress = album_progress(session, task)
    if not progress:
        return groups
    sources = session.scalars(select(ChannelMessage).where(ChannelMessage.tenant_id == task.tenant_id,
        ChannelMessage.grouped_id.in_(progress),
        ChannelMessage.channel_target_id == int((task.type_config or {})["target_channel_id"])))
    albums = {(row.channel_target_id, row.message_id): row.grouped_id for row in sources}
    merged, result = {}, []
    for group in groups:
        album = albums.get((group.get("channel_target_id"), group.get("message_id")))
        if not album:
            result.append(group)
            continue
        current = merged.setdefault(album, {**group, "actions": [], "album_id": album})
        current["actions"].extend(group["actions"])
    return result + [_apply_progress(group, progress[album]) for album, group in merged.items()]


def _apply_progress(group, progress):
    target = progress["target_count"]
    status = "目标未证明" if target is None else "运行中"
    if target is not None and progress["unknown_accounts"]:
        status = "结果待确认"
    elif target is not None and progress["completed_count"] >= target:
        status = "已完成"
    elif progress["failed_count"]:
        status = "部分未完成"
    return {**group, **progress, "target_count": target if target is not None else 0,
        "target_count_proven": target is not None, "action_label": "相册点赞（账号）", "subtask_status": status,
        "stats": {**group["stats"], "target": target,
            "total": progress["planned_child_rpc"], "success": progress["confirmed_child_reactions"],
            "remote_confirmed": progress["confirmed_child_reactions"]}}
