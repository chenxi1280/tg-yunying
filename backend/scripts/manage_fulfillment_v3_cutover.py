from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    Action,
    FulfillmentRemoteFact,
    RemoteMutationTombstone,
    Task,
    TaskContractActivationManifest,
    TaskDeleteOperation,
)
from app.services.task_center.fulfillment_activation import (
    ActivationRequest,
    activate_manifest,
    clone_prepared_task,
    prepare_activation_manifest,
    preview_activation,
)
from app.services.task_center.physical_task_deletion import (
    DeleteRequest,
    advance_task_deletion,
    prepare_task_deletion,
)


CREATE_CONFIRMATION = "CREATE_NEW_TASKS_RESET_SAME_DAY_PROGRESS"
ACTIVATE_CONFIRMATION = "ACTIVATE_FACT_FIRST_V3_ROUTE"
DELETE_CONFIRMATION = "DELETE_OLD_TASK_AFTER_ROUTE_ACTIVATION"
DELETE_STAGE_LIMIT = 6


def _ids(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _overrides(value: str) -> dict[str, int]:
    payload = json.loads(value or "{}")
    if not isinstance(payload, dict):
        raise ValueError("daily target overrides must be a JSON object")
    return {str(key): int(target) for key, target in payload.items()}


def _task_payload(task: Task) -> dict:
    config = dict(task.type_config or {})
    return {
        "id": task.id,
        "name": task.name,
        "type": task.type,
        "status": task.status,
        "contract": task.fulfillment_contract_version,
        "lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
        "target_id": config.get("target_group_id")
        or config.get("target_channel_id")
        or config.get("target_operation_target_id"),
        "daily_target": config.get("daily_message_target")
        or config.get("daily_click_target_count"),
    }


def inventory(args) -> dict:
    with SessionLocal() as session:
        tasks = list(session.scalars(select(Task).where(
            Task.tenant_id == args.tenant_id,
            Task.deleted_at.is_(None),
        ).order_by(Task.type, Task.created_at)))
        return {
            "tenant_id": args.tenant_id,
            "legacy": [_task_payload(task) for task in tasks if task.fulfillment_contract_version != "fact_first_v3"],
            "fact_first_v3": [_task_payload(task) for task in tasks if task.fulfillment_contract_version == "fact_first_v3"],
        }


def create_prepared(args) -> dict:
    _require_confirmation(args.confirm, CREATE_CONFIRMATION)
    source_ids = _ids(args.source_task_ids)
    target_overrides = _overrides(args.daily_target_overrides_json)
    with SessionLocal() as session:
        sources = _exact_tasks(session, args.tenant_id, source_ids)
        mapping = {}
        for source in sources:
            clone = clone_prepared_task(session, source, actor_id=args.actor_id)
            clone.name = source.name
            clone.type_config = _new_type_config(source, target_overrides)
            mapping[source.id] = clone.id
        session.flush()
        session.commit()
        return {
            "same_day_recreate_resets_progress": True,
            "source_to_new_task": mapping,
            "new_task_ids": list(mapping.values()),
        }


def prepare_manifest(args) -> dict:
    old_ids = _ids(args.old_task_ids)
    new_ids = _ids(args.new_task_ids)
    with SessionLocal() as session:
        preview = preview_activation(
            session,
            tenant_id=args.tenant_id,
            old_task_ids=old_ids,
            new_task_ids=new_ids,
        )
        if not args.apply:
            return {"apply": False, **asdict(preview)}
        _require_confirmation(args.confirm, ACTIVATE_CONFIRMATION)
        manifest = prepare_activation_manifest(session, ActivationRequest(
            tenant_id=args.tenant_id,
            release_train=args.release_train,
            old_task_ids=old_ids,
            new_task_ids=new_ids,
            canary_task_id=args.canary_task_id,
            expected_old_set_hash=args.expected_old_set_hash,
            expected_new_config_set_hash=args.expected_new_config_set_hash,
            approval_ref=args.approval_ref,
        ))
        session.commit()
        return _manifest_payload(manifest)


def activate(args) -> dict:
    _require_confirmation(args.confirm, ACTIVATE_CONFIRMATION)
    with SessionLocal() as session:
        manifest = activate_manifest(
            session,
            args.manifest_id,
            expected_version=args.expected_version,
        )
        session.commit()
        return _manifest_payload(manifest)


def start_delete(args) -> dict:
    _require_confirmation(args.confirm, DELETE_CONFIRMATION)
    with SessionLocal() as session:
        operation = prepare_task_deletion(session, DeleteRequest(
            task_id=args.task_id,
            manifest_id=args.manifest_id,
            expected_manifest_hash=args.expected_manifest_hash,
            actor=args.actor,
            approval_ref=args.approval_ref,
        ))
        session.commit()
        return _delete_payload(operation)


def advance_delete(args) -> dict:
    _require_confirmation(args.confirm, DELETE_CONFIRMATION)
    with SessionLocal() as session:
        operation = advance_task_deletion(
            session,
            args.operation_id,
            expected_stage_version=args.expected_stage_version,
        )
        session.commit()
        return _delete_payload(operation)


def delete_manifest(args) -> dict:
    _require_confirmation(args.confirm, DELETE_CONFIRMATION)
    with SessionLocal() as session:
        manifest = session.get(TaskContractActivationManifest, args.manifest_id)
        if manifest is None or manifest.state != "active":
            raise ValueError("physical_delete_manifest_not_active")
        if manifest.old_set_hash != args.expected_manifest_hash:
            raise ValueError("physical_delete_manifest_hash_mismatch")
        task_ids = tuple(manifest.old_task_ids or ())
    return {
        "manifest_id": args.manifest_id,
        "operations": [_delete_one(args, task_id) for task_id in task_ids],
    }


def _delete_one(args, task_id: str) -> dict:
    operation_id = _existing_delete_operation(task_id)
    if operation_id is None:
        with SessionLocal() as session:
            operation = prepare_task_deletion(session, DeleteRequest(
                task_id=task_id,
                manifest_id=args.manifest_id,
                expected_manifest_hash=args.expected_manifest_hash,
                actor=args.actor,
                approval_ref=args.approval_ref,
            ))
            session.commit()
            operation_id = operation.id
    for _ in range(DELETE_STAGE_LIMIT):
        with SessionLocal() as session:
            operation = session.get(TaskDeleteOperation, operation_id)
            if operation is None:
                raise RuntimeError("physical_delete_operation_missing")
            if operation.state == "committed":
                return _delete_payload(operation)
            operation = advance_task_deletion(
                session,
                operation.id,
                expected_stage_version=int(operation.stage_version or 1),
            )
            session.commit()
    raise RuntimeError("physical_delete_stage_limit_exceeded")


def _existing_delete_operation(task_id: str) -> str | None:
    with SessionLocal() as session:
        return session.scalar(select(TaskDeleteOperation.id).where(
            TaskDeleteOperation.original_task_id == task_id,
        ).order_by(TaskDeleteOperation.created_at.desc()).limit(1))


def verify(args) -> dict:
    with SessionLocal() as session:
        manifest = session.get(TaskContractActivationManifest, args.manifest_id)
        if manifest is None:
            raise ValueError("manifest_not_found")
        new_rows = [_verify_new_task(session, task_id) for task_id in manifest.new_task_ids]
        old_rows = [_verify_old_task(session, task_id) for task_id in manifest.old_task_ids]
        return {"manifest": _manifest_payload(manifest), "new_tasks": new_rows, "old_tasks": old_rows}


def _verify_new_task(session, task_id: str) -> dict:
    task = session.get(Task, task_id)
    fact_count = session.scalar(select(func.count(FulfillmentRemoteFact.fact_id)).where(
        FulfillmentRemoteFact.task_id == task_id,
        FulfillmentRemoteFact.fact_kind.not_in(("remote_outcome_unknown", "safely_not_executed")),
    ))
    return {
        "task_id": task_id,
        "exists": task is not None,
        "status": task.status if task else "missing",
        "confirmed_remote_fact_count": int(fact_count or 0),
    }


def _verify_old_task(session, task_id: str) -> dict:
    action_count = session.scalar(select(func.count(Action.id)).where(Action.task_id == task_id))
    tombstone_count = session.scalar(select(func.count(RemoteMutationTombstone.id)).where(
        RemoteMutationTombstone.original_task_id == task_id
    ))
    operation = session.scalar(select(TaskDeleteOperation).where(
        TaskDeleteOperation.original_task_id == task_id
    ).order_by(TaskDeleteOperation.created_at.desc()).limit(1))
    return {
        "task_id": task_id,
        "exists": session.get(Task, task_id) is not None,
        "runtime_action_count": int(action_count or 0),
        "tombstone_count": int(tombstone_count or 0),
        "delete_state": operation.state if operation else "not_started",
    }


def _exact_tasks(session, tenant_id: int, task_ids: tuple[str, ...]) -> list[Task]:
    tasks = list(session.scalars(select(Task).where(
        Task.tenant_id == tenant_id,
        Task.id.in_(task_ids),
    )))
    if {task.id for task in tasks} != set(task_ids):
        raise ValueError("task_set_mismatch")
    if any(task.fulfillment_contract_version == "fact_first_v3" for task in tasks):
        raise ValueError("source_task_must_be_legacy")
    return tasks


def _new_type_config(source: Task, overrides: dict[str, int]) -> dict:
    config = dict(source.type_config or {})
    target = overrides.get(source.id)
    if target is not None:
        if source.type != "group_ai_chat" or target <= 0:
            raise ValueError("daily target override only supports positive group_ai_chat target")
        config["daily_message_target"] = target
    return config


def _manifest_payload(manifest: TaskContractActivationManifest) -> dict:
    return {
        "id": manifest.id,
        "state": manifest.state,
        "version": int(manifest.version or 1),
        "old_task_ids": list(manifest.old_task_ids or []),
        "new_task_ids": list(manifest.new_task_ids or []),
        "canary_task_id": manifest.canary_task_id,
        "old_set_hash": manifest.old_set_hash,
        "new_config_set_hash": manifest.new_config_set_hash,
    }


def _delete_payload(operation: TaskDeleteOperation) -> dict:
    return {
        "id": operation.id,
        "task_id": operation.original_task_id,
        "state": operation.state,
        "stage_version": int(operation.stage_version or 1),
        "resume_stage": operation.resume_stage,
        "counts": dict(operation.counts or {}),
    }


def _require_confirmation(actual: str, expected: str) -> None:
    if actual != expected:
        raise ValueError(f"confirmation_required:{expected}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", type=int, default=1)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory").set_defaults(handler=inventory)
    create = sub.add_parser("create-prepared")
    create.add_argument("--source-task-ids", required=True)
    create.add_argument("--daily-target-overrides-json", default="{}")
    create.add_argument("--actor-id", type=int)
    create.add_argument("--confirm", required=True)
    create.set_defaults(handler=create_prepared)
    _add_manifest_commands(sub)
    _add_delete_commands(sub)
    verify_cmd = sub.add_parser("verify")
    verify_cmd.add_argument("--manifest-id", required=True)
    verify_cmd.set_defaults(handler=verify)
    return parser


def _add_manifest_commands(sub) -> None:
    prepare = sub.add_parser("prepare-manifest")
    prepare.add_argument("--old-task-ids", required=True)
    prepare.add_argument("--new-task-ids", required=True)
    prepare.add_argument("--canary-task-id", required=True)
    prepare.add_argument("--release-train", required=True)
    prepare.add_argument("--approval-ref", required=True)
    prepare.add_argument("--expected-old-set-hash", default="")
    prepare.add_argument("--expected-new-config-set-hash", default="")
    prepare.add_argument("--apply", action="store_true")
    prepare.add_argument("--confirm", default="")
    prepare.set_defaults(handler=prepare_manifest)
    activate_cmd = sub.add_parser("activate")
    activate_cmd.add_argument("--manifest-id", required=True)
    activate_cmd.add_argument("--expected-version", type=int, required=True)
    activate_cmd.add_argument("--confirm", required=True)
    activate_cmd.set_defaults(handler=activate)


def _add_delete_commands(sub) -> None:
    delete_start = sub.add_parser("delete-start")
    delete_start.add_argument("--task-id", required=True)
    delete_start.add_argument("--manifest-id", required=True)
    delete_start.add_argument("--expected-manifest-hash", required=True)
    delete_start.add_argument("--actor", required=True)
    delete_start.add_argument("--approval-ref", required=True)
    delete_start.add_argument("--confirm", required=True)
    delete_start.set_defaults(handler=start_delete)
    delete_advance = sub.add_parser("delete-advance")
    delete_advance.add_argument("--operation-id", required=True)
    delete_advance.add_argument("--expected-stage-version", type=int, required=True)
    delete_advance.add_argument("--confirm", required=True)
    delete_advance.set_defaults(handler=advance_delete)
    delete_all = sub.add_parser("delete-manifest")
    delete_all.add_argument("--manifest-id", required=True)
    delete_all.add_argument("--expected-manifest-hash", required=True)
    delete_all.add_argument("--actor", required=True)
    delete_all.add_argument("--approval-ref", required=True)
    delete_all.add_argument("--confirm", required=True)
    delete_all.set_defaults(handler=delete_manifest)


def main() -> None:
    args = _parser().parse_args()
    print(json.dumps(args.handler(args), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
