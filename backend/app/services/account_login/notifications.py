from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.admin_chats import send_admin_chat_broadcast
from app.models import (
    Tenant,
    TgAccountLoginBatch,
    TgAccountLoginBatchItem,
    TgAccountLoginBatchNotification,
)
from app.security import decrypt_secret
from app.services._common import _now, audit
from app.services.notifications import NotificationResult, send_telegram_bot_message

from .batches import TERMINAL_ITEM_STATUSES
from .contracts import BatchLoginError


MAX_DELIVERY_ATTEMPTS = 5
MAX_BOT_MESSAGE_CHARACTERS = 3500
OUTBOX_CLAIM_SECONDS = 90


@dataclass(frozen=True)
class BotDelivery:
    notification_id: int
    tenant_id: int
    bot_token: str
    chat_ids: str
    message: str


def finalize_batch_if_terminal(session: Session, batch_id: int) -> bool:
    batch = session.scalar(select(TgAccountLoginBatch).where(TgAccountLoginBatch.id == batch_id).with_for_update())
    if not batch:
        return False
    items = list(session.scalars(select(TgAccountLoginBatchItem).where(
        TgAccountLoginBatchItem.batch_id == batch.id,
    ).order_by(TgAccountLoginBatchItem.line_no).with_for_update()))
    _recount_batch(batch, items)
    if not items or any(item.status not in TERMINAL_ITEM_STATUSES for item in items):
        return False
    previous_status = batch.status
    batch.status = _terminal_batch_status(batch)
    batch.finished_at = batch.finished_at or _now()
    batch.state_version += int(previous_status != batch.status)
    _insert_notifications(session, batch, items, "initial")
    return True


def record_batch_correction(
    session: Session,
    batch_id: int,
    changed_item_id: int | None = None,
    previous_status: str = "unresolved",
) -> None:
    batch = session.scalar(select(TgAccountLoginBatch).where(TgAccountLoginBatch.id == batch_id).with_for_update())
    if not batch:
        raise BatchLoginError("not_found", "批量登录任务不存在")
    items = list(session.scalars(select(TgAccountLoginBatchItem).where(
        TgAccountLoginBatchItem.batch_id == batch.id,
    ).order_by(TgAccountLoginBatchItem.line_no).with_for_update()))
    _recount_batch(batch, items)
    batch.resolution_version += 1
    if batch.status != "cancelled":
        batch.status = "completed_with_unresolved" if batch.unresolved_count else "completed"
    batch.state_version += 1
    corrections = _correction_summaries(items, changed_item_id, previous_status)
    _insert_notifications(session, batch, items, "correction", corrections)


def _recount_batch(batch: TgAccountLoginBatch, items: list[TgAccountLoginBatchItem]) -> None:
    batch.total_count = len(items)
    batch.success_count = sum(item.status in {"succeeded", "succeeded_with_warning"} for item in items)
    batch.failed_count = sum(item.status == "failed" for item in items)
    batch.unresolved_count = sum(item.status == "unresolved" for item in items)
    batch.warning_count = sum(item.status == "succeeded_with_warning" for item in items)
    batch.skipped_count = sum(item.status == "skipped" for item in items)


def _terminal_batch_status(batch: TgAccountLoginBatch) -> str:
    if batch.status == "cancelling":
        return "cancelled"
    if batch.unresolved_count:
        return "completed_with_unresolved"
    return "completed"


def _insert_notifications(
    session: Session,
    batch: TgAccountLoginBatch,
    items: list[TgAccountLoginBatchItem],
    event_type: str,
    corrections: list[dict[str, object]] | None = None,
) -> None:
    summary = json.dumps(_notification_summary(batch, items, corrections), ensure_ascii=False, separators=(",", ":"))
    for channel in ("platform", "tg_bot"):
        exists = session.scalar(select(TgAccountLoginBatchNotification.id).where(
            TgAccountLoginBatchNotification.batch_id == batch.id,
            TgAccountLoginBatchNotification.execution_generation == batch.execution_generation,
            TgAccountLoginBatchNotification.resolution_version == batch.resolution_version,
            TgAccountLoginBatchNotification.channel == channel,
            TgAccountLoginBatchNotification.recipient_user_id == batch.recipient_user_id,
        ))
        if exists:
            continue
        session.add(TgAccountLoginBatchNotification(
            batch_id=batch.id,
            tenant_id=batch.tenant_id,
            execution_generation=batch.execution_generation,
            resolution_version=batch.resolution_version,
            event_type=event_type,
            channel=channel,
            recipient_user_id=batch.recipient_user_id,
            summary_json=summary,
            delivery_status="ready" if channel == "platform" else "pending",
        ))


def _notification_summary(
    batch: TgAccountLoginBatch,
    items: list[TgAccountLoginBatchItem],
    corrections: list[dict[str, object]] | None,
) -> dict[str, object]:
    return {
        "batch_id": batch.id,
        "status": batch.status,
        "counts": {
            "total": batch.total_count,
            "success": batch.success_count,
            "failed": batch.failed_count,
            "unresolved": batch.unresolved_count,
            "warning": batch.warning_count,
            "skipped": batch.skipped_count,
        },
        "failed": _item_summaries(items, {"failed"}),
        "unresolved": _item_summaries(items, {"unresolved"}),
        "warning": _item_summaries(items, {"succeeded_with_warning"}),
        "corrections": corrections or [],
    }


def _correction_summaries(
    items: list[TgAccountLoginBatchItem],
    changed_item_id: int | None,
    previous_status: str,
) -> list[dict[str, object]]:
    item = next((value for value in items if value.id == changed_item_id), None)
    if not item:
        return []
    return [{
        "line_no": item.line_no,
        "phone_masked": item.phone_masked,
        "from_status": previous_status,
        "to_status": item.status,
        "reason": item.failure_type or item.warning_detail or item.phase,
    }]


def _item_summaries(items: list[TgAccountLoginBatchItem], statuses: set[str]) -> list[dict[str, object]]:
    return [
        {
            "line_no": item.line_no,
            "phone_masked": item.phone_masked,
            "reason": item.failure_type or item.warning_detail,
        }
        for item in items if item.status in statuses
    ]


def list_platform_notifications(
    session: Session,
    tenant_id: int,
    user_id: int,
    *,
    unacknowledged: bool,
) -> list[dict[str, object]]:
    latest_initial_ids = select(func.max(TgAccountLoginBatchNotification.id)).where(
        TgAccountLoginBatchNotification.tenant_id == tenant_id,
        TgAccountLoginBatchNotification.recipient_user_id == user_id,
        TgAccountLoginBatchNotification.channel == "platform",
        TgAccountLoginBatchNotification.event_type == "initial",
    ).group_by(TgAccountLoginBatchNotification.batch_id)
    query = select(TgAccountLoginBatchNotification).where(
        TgAccountLoginBatchNotification.tenant_id == tenant_id,
        TgAccountLoginBatchNotification.recipient_user_id == user_id,
        TgAccountLoginBatchNotification.channel == "platform",
        or_(
            TgAccountLoginBatchNotification.event_type != "initial",
            TgAccountLoginBatchNotification.id.in_(latest_initial_ids),
        ),
    )
    if unacknowledged:
        query = query.where(TgAccountLoginBatchNotification.acknowledged_at.is_(None))
    rows = session.scalars(query.order_by(TgAccountLoginBatchNotification.id.desc()).limit(100))
    return [_notification_out(row) for row in rows]


def _notification_out(row: TgAccountLoginBatchNotification) -> dict[str, object]:
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "execution_generation": row.execution_generation,
        "resolution_version": row.resolution_version,
        "event_type": row.event_type,
        "channel": row.channel,
        "summary": json.loads(row.summary_json),
        "delivery_status": row.delivery_status,
        "acknowledged_at": row.acknowledged_at,
        "state_version": row.state_version,
        "created_at": row.created_at,
    }


def acknowledge_notification(
    session: Session,
    tenant_id: int,
    user_id: int,
    notification_id: int,
    expected_version: int,
) -> dict[str, object]:
    row = session.scalar(select(TgAccountLoginBatchNotification).where(
        TgAccountLoginBatchNotification.id == notification_id,
    ).with_for_update())
    if not row or row.tenant_id != tenant_id or row.recipient_user_id != user_id or row.channel != "platform":
        raise BatchLoginError("not_found", "批量登录提醒不存在")
    if row.acknowledged_at and expected_version == row.state_version - 1:
        return _notification_out(row)
    if row.state_version != expected_version:
        raise BatchLoginError("state_conflict", "提醒状态已变化")
    row.acknowledged_at = _now()
    row.state_version += 1
    session.commit()
    session.refresh(row)
    return _notification_out(row)


def drain_notification_outbox(
    session_factory,
    limit: int,
    *,
    sender: Callable[[str, str, str], NotificationResult] = send_telegram_bot_message,
) -> int:
    processed = 0
    for _ in range(max(1, limit)):
        delivery = _claim_bot_delivery(session_factory)
        if not delivery:
            break
        summary = send_admin_chat_broadcast(
            bot_token=delivery.bot_token,
            raw_admin_chat_id=delivery.chat_ids,
            text=delivery.message,
            sender=sender,
        )
        _record_bot_delivery(session_factory, delivery, NotificationResult(summary.ok, summary.detail))
        processed += 1
    return processed


def _claim_bot_delivery(session_factory) -> BotDelivery | None:
    with session_factory() as session:
        now = _now()
        row = session.scalar(select(TgAccountLoginBatchNotification).where(
            TgAccountLoginBatchNotification.channel == "tg_bot",
            or_(
                and_(
                    TgAccountLoginBatchNotification.delivery_status.in_(("pending", "retry")),
                    (TgAccountLoginBatchNotification.next_retry_at.is_(None) | (TgAccountLoginBatchNotification.next_retry_at <= now)),
                ),
                and_(
                    TgAccountLoginBatchNotification.delivery_status == "sending",
                    TgAccountLoginBatchNotification.next_retry_at <= now,
                ),
            ),
        ).order_by(TgAccountLoginBatchNotification.id).with_for_update(skip_locked=True))
        if not row:
            return None
        tenant = session.get(Tenant, row.tenant_id)
        token = decrypt_secret(tenant.telegram_bot_token_ciphertext) if tenant else ""
        if not tenant or not token or not tenant.admin_chat_id:
            _mark_delivery_unconfigured(session, row)
            session.commit()
            return None
        row.delivery_status = "sending"
        row.delivery_attempts += 1
        row.next_retry_at = now + timedelta(seconds=OUTBOX_CLAIM_SECONDS)
        message = _bot_message(row)
        delivery = BotDelivery(row.id, row.tenant_id, token, tenant.admin_chat_id, message)
        session.commit()
        return delivery


def _bot_message(row: TgAccountLoginBatchNotification) -> str:
    summary = json.loads(row.summary_json)
    counts = summary.get("counts", {})
    prefix = "批量登录结果更正" if row.event_type == "correction" else "批量登录完成"
    header = (
        f"{prefix} #{row.batch_id}\n"
        f"成功 {counts.get('success', 0)} / 失败 {counts.get('failed', 0)} / "
        f"未解 {counts.get('unresolved', 0)} / 警告 {counts.get('warning', 0)}"
    )
    message = "\n".join([header, *_bot_detail_sections(summary)])
    if len(message) <= MAX_BOT_MESSAGE_CHARACTERS:
        return message
    return f"{message[:MAX_BOT_MESSAGE_CHARACTERS]}\n…详情过长，请到平台查看完整清单"


def _bot_detail_sections(summary: dict) -> list[str]:
    sections: list[str] = []
    for key, label in (("failed", "失败"), ("unresolved", "未解"), ("warning", "警告")):
        items = summary.get(key, [])
        if not items:
            continue
        values = [f"第{item.get('line_no')}行 {item.get('phone_masked')}（{item.get('reason')}）" for item in items]
        sections.append(f"{label}：" + "、".join(values))
    for item in summary.get("corrections", []):
        sections.append(
            f"更正：第{item.get('line_no')}行 {item.get('phone_masked')} "
            f"{item.get('from_status')}→{item.get('to_status')}（{item.get('reason')}）"
        )
    return sections


def _mark_delivery_unconfigured(session: Session, row: TgAccountLoginBatchNotification) -> None:
    row.delivery_status = "dead_letter"
    row.next_retry_at = None
    row.last_error = "Telegram Bot token or admin chat id not configured"
    _expose_tg_dead_letter(session, row)
    audit(session, tenant_id=row.tenant_id, actor="account-login-notifier", action="批量登录TG提醒死信", target_type="tg_account_login_batch_notification", target_id=str(row.id), detail="bot_not_configured")


def _record_bot_delivery(session_factory, delivery: BotDelivery, result: NotificationResult) -> None:
    with session_factory() as session:
        row = session.get(TgAccountLoginBatchNotification, delivery.notification_id)
        if not row or row.delivery_status != "sending":
            return
        if result.ok:
            row.delivery_status = "sent"
            row.sent_at = _now()
            row.next_retry_at = None
            row.last_error = ""
        elif row.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
            row.delivery_status = "dead_letter"
            row.next_retry_at = None
            row.last_error = result.detail[:500]
            _expose_tg_dead_letter(session, row)
            audit(session, tenant_id=delivery.tenant_id, actor="account-login-notifier", action="批量登录TG提醒死信", target_type="tg_account_login_batch_notification", target_id=str(row.id), detail="retry_exhausted")
        else:
            row.delivery_status = "retry"
            row.last_error = result.detail[:500]
            row.next_retry_at = _now() + timedelta(seconds=2 ** row.delivery_attempts * 30)
        session.commit()


def _expose_tg_dead_letter(session: Session, row: TgAccountLoginBatchNotification) -> None:
    platform = session.scalar(select(TgAccountLoginBatchNotification).where(
        TgAccountLoginBatchNotification.batch_id == row.batch_id,
        TgAccountLoginBatchNotification.execution_generation == row.execution_generation,
        TgAccountLoginBatchNotification.resolution_version == row.resolution_version,
        TgAccountLoginBatchNotification.channel == "platform",
        TgAccountLoginBatchNotification.recipient_user_id == row.recipient_user_id,
    ).with_for_update())
    if not platform:
        return
    summary = json.loads(platform.summary_json)
    summary["tg_bot_delivery"] = "dead_letter"
    platform.summary_json = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    platform.state_version += 1


__all__ = [
    "acknowledge_notification",
    "drain_notification_outbox",
    "finalize_batch_if_terminal",
    "list_platform_notifications",
    "record_batch_correction",
]
