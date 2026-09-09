"""Read-only inventory of text evidence and exact dependent production objects."""

import hashlib
import json

from sqlalchemy import select

from app.content_safety import SCREENING_VERSION, content_fingerprint, content_screening_reason
from app.models import ChannelMessage, GroupContextMessage, OperationTarget, Task, TenantLearningSource, TgGroup


READ_BATCH_SIZE = 500


def snapshot_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def target_snapshot(target) -> dict:
    return {"id": target.id, "tenant_id": target.tenant_id, "status": target.lifecycle_status,
            "version": int(target.lifecycle_version or 1), "reference_revision": target.reference_revision,
            "identity_hash": snapshot_hash([target.tg_peer_id, target.username])}


def task_snapshot(task) -> dict:
    return {"id": task.id, "status": task.status, "epoch": task.task_lifecycle_epoch,
            "config_hash": snapshot_hash([task.type_config, task.group_ai_prejoin_channel_ids])}


def screening_inventory(session, tenant_id: int) -> dict:
    targets = list(session.scalars(select(OperationTarget).where(OperationTarget.tenant_id == tenant_id).order_by(OperationTarget.id)))
    groups = list(session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id).order_by(TgGroup.id)))
    peer_targets = {target.tg_peer_id: target.id for target in targets}
    group_targets = {group.id: peer_targets.get(group.tg_peer_id) for group in groups}
    evidence, inspected = _text_evidence(session, tenant_id, group_targets)
    ids = {row["target_id"] for row in evidence if row["target_id"] is not None}
    affected = [target for target in targets if target.id in ids]
    affected_groups = [group for group in groups if group_targets.get(group.id) in ids]
    tasks = session.scalars(select(Task).where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None)).order_by(Task.id))
    dependent = [task_snapshot(task) for task in tasks if _task_depends(task, affected, affected_groups)]
    sources = session.scalars(select(TenantLearningSource).where(
        TenantLearningSource.tenant_id == tenant_id, TenantLearningSource.target_id.in_(ids)).order_by(TenantLearningSource.id))
    report = {"tenant_id": tenant_id, "rule_version": SCREENING_VERSION, "inspected": inspected,
              "evidence": evidence, "targets": [target_snapshot(row) for row in affected],
              "tasks": dependent, "groups": [_group_snapshot(row) for row in affected_groups],
              "learning_sources": [_source_snapshot(row) for row in sources]}
    return {**report, "fingerprint": snapshot_hash(report)}


def _text_evidence(session, tenant_id, group_targets):
    queries = (
        ("channel_message", select(ChannelMessage.id, ChannelMessage.channel_target_id, ChannelMessage.content_preview)
         .where(ChannelMessage.tenant_id == tenant_id)),
        ("group_context", select(GroupContextMessage.id, GroupContextMessage.group_id, GroupContextMessage.content)
         .where(GroupContextMessage.tenant_id == tenant_id)),
    )
    evidence, inspected = [], {}
    for kind, query in queries:
        inspected[kind] = 0
        for source_id, parent_id, content in session.execute(query.execution_options(yield_per=READ_BATCH_SIZE)):
            inspected[kind] += 1
            if not content_screening_reason(content):
                continue
            target_id = group_targets.get(parent_id) if kind == "group_context" else parent_id
            evidence.append({"kind": kind, "id": source_id, "target_id": target_id,
                             "content_hash": content_fingerprint(content)})
    return sorted(evidence, key=lambda row: (row["kind"], row["id"])), inspected


def _task_depends(task, targets, groups) -> bool:
    config = task.type_config or {}
    ids, group_ids = {row.id for row in targets}, {row.id for row in groups}
    target_fields = ("target_channel_id", "target_operation_target_id", "operation_target_id")
    if any(str(config.get(key)) in {str(value) for value in ids} for key in target_fields):
        return True
    if any(str(value) in {str(item) for item in ids}
           for value in config.get("target_operation_target_ids", ())):
        return True
    if any(str(value) in {str(item) for item in group_ids}
           for value in config.get("target_group_ids", ())):
        return True
    if str(config.get("target_group_id")) in {str(value) for value in group_ids}:
        return True
    refs = {str(value).removeprefix("https://t.me/").lstrip("@").strip("/")
            for value in task.group_ai_prejoin_channel_ids or ()}
    return bool(refs & {row.username for row in targets})


def _group_snapshot(group) -> dict:
    return {"id": group.id, "listener_enabled": group.listener_enabled,
            "listener_auto_reply_enabled": group.listener_auto_reply_enabled}


def _source_snapshot(source) -> dict:
    return {"id": source.id, "is_enabled": source.is_enabled,
            "auto_sync_enabled": source.auto_sync_enabled, "status": source.source_status}
