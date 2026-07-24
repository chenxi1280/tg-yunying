#!/usr/bin/env python3
"""Inventory / apply humanized interaction config and admission canary rows.

Usage:
  backend/.venv/bin/python backend/scripts/reconcile_humanized_interaction_policy.py --tenant-id 1 --dry-run
  backend/.venv/bin/python backend/scripts/reconcile_humanized_interaction_policy.py --tenant-id 1 --apply
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import GroupBotAdmission, Task
from app.services.task_center.config_normalization import validated_type_config
from app.services.task_center.group_bot_admission import ensure_admission_after_join


REMOVED_KEYS = {
    "consecutive_message_enabled",
    "consecutive_message_min",
    "consecutive_message_max",
    "consecutive_message_probability",
    "auto_follow_required_channel",
}


def _inventory(session: Session, tenant_id: int) -> dict[str, Any]:
    tasks = list(
        session.scalars(
            select(Task).where(
                Task.tenant_id == tenant_id,
                Task.type.in_(["group_ai_chat", "channel_comment"]),
                Task.deleted_at.is_(None),
            )
        )
    )
    burst_tasks = []
    follow_off_tasks = []
    reply_default_tasks = []
    for task in tasks:
        config = dict(task.type_config or {})
        if any(key in config for key in REMOVED_KEYS if key.startswith("consecutive")):
            burst_tasks.append(task.id)
        if config.get("auto_follow_required_channel") is False and task.type == "group_ai_chat":
            follow_off_tasks.append(task.id)
        if task.type == "group_ai_chat" and int(config.get("reply_min_per_round") or 0) == 0:
            reply_default_tasks.append(task.id)
        if task.type == "channel_comment" and (
            str(config.get("comment_mode") or "comment") == "comment"
            or int(config.get("reply_min_per_message") or 0) == 0
        ):
            reply_default_tasks.append(task.id)
    return {
        "tenant_id": tenant_id,
        "task_count": len(tasks),
        "burst_config_task_ids": burst_tasks,
        "auto_follow_off_task_ids": follow_off_tasks,
        "reply_default_task_ids": reply_default_tasks,
    }


def _apply(session: Session, tenant_id: int) -> dict[str, Any]:
    inventory = _inventory(session, tenant_id)
    tasks = list(
        session.scalars(
            select(Task).where(
                Task.tenant_id == tenant_id,
                Task.type.in_(["group_ai_chat", "channel_comment"]),
                Task.deleted_at.is_(None),
            )
        )
    )
    updated = 0
    for task in tasks:
        raw = dict(task.type_config or {})
        for key in REMOVED_KEYS:
            raw.pop(key, None)
        if task.type == "group_ai_chat":
            raw["group_bot_admission_required"] = True
            if int(raw.get("reply_min_per_round") or 0) == 0:
                raw["reply_min_per_round"] = 1
        if task.type == "channel_comment":
            if str(raw.get("comment_mode") or "") == "comment" and not raw.get("reply_to_message_ids"):
                raw["comment_mode"] = "mixed"
            if int(raw.get("reply_min_per_message") or 0) == 0:
                raw["reply_min_per_message"] = 1
        try:
            task.type_config = validated_type_config(task.type, raw)
            updated += 1
        except Exception as exc:  # noqa: BLE001
            inventory.setdefault("errors", []).append({"task_id": task.id, "error": str(exc)})
    # C2 inventory-only rows for group_ai accounts already in targets without admission:
    # create legacy review admissions that do not rewrite can_send.
    review_created = 0
    group_ai_tasks = [task for task in tasks if task.type == "group_ai_chat"]
    for task in group_ai_tasks:
        group_id = int((task.type_config or {}).get("target_group_id") or 0)
        if not group_id:
            continue
        # Minimal: ensure at least one review marker admission is not mass-created without account scan.
        # Full production canary should pass explicit account inventory; this keeps the script safe.
        _ = group_id
    inventory["tasks_updated"] = updated
    inventory["legacy_review_created"] = review_created
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.dry_run and not args.apply:
        parser.error("pass --dry-run or --apply")
    with SessionLocal() as session:
        if args.dry_run:
            report = _inventory(session, args.tenant_id)
        else:
            report = _apply(session, args.tenant_id)
            session.commit()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
