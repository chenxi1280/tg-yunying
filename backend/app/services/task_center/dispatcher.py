from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.gateways import OutboundSegment
from app.models import AccountStatus, Action, FailureType, OperationTarget, ReviewQueue, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now, gateway
from app.services.developer_apps import credentials_for_account

from .payloads import LikeMessagePayload, PostCommentPayload, SendMessagePayload, ViewMessagePayload, payload_error_message, validate_action_payload
from .policies import validate_group_send_policy
from .review import has_pending_review


def dispatch_action(session: Session, action: Action) -> bool:
    if has_pending_review(session, action.id):
        return False
    account = session.get(TgAccount, action.account_id) if action.account_id else None
    if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
        _fail(action, FailureType.ACCOUNT_UNAVAILABLE.value, "账号不可用")
        return True
    try:
        payload = validate_action_payload(action.action_type, action.payload or {})
        credentials = credentials_for_account(session, account)
        if action.action_type == "send_message":
            return _dispatch_send_message(session, action, account, credentials, payload)
        if action.action_type == "view_message":
            return _dispatch_view(action, account, credentials, session, payload)
        if action.action_type == "like_message":
            return _dispatch_like(action, account, credentials, session, payload)
        if action.action_type == "post_comment":
            return _dispatch_comment(action, account, credentials, session, payload)
        _fail(action, FailureType.UNKNOWN.value, f"未知 action_type: {action.action_type}")
        return True
    except (ValidationError, ValueError) as exc:
        _fail(action, FailureType.UNKNOWN.value, payload_error_message(exc))
        return True
    except Exception as exc:  # noqa: BLE001 - worker must keep draining.
        _fail(action, FailureType.UNKNOWN.value, str(exc))
        return True


def due_actions(session: Session, limit: int = 100) -> list[Action]:
    pending_review_exists = (
        select(ReviewQueue.id)
        .where(ReviewQueue.action_id == Action.id, ReviewQueue.status == "pending")
        .exists()
    )
    return list(
        session.scalars(
            select(Action)
            .join(Task, Task.id == Action.task_id)
            .where(
                Action.status == "pending",
                Action.scheduled_at <= _now(),
                Task.status == "running",
                ~pending_review_exists,
            )
            .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
            .limit(limit)
        )
    )


def _dispatch_send_message(session: Session, action: Action, account: TgAccount, credentials, payload: SendMessagePayload) -> bool:
    group_id = payload.group_id
    content = payload.message_text
    if group_id:
        group = session.get(TgGroup, group_id)
        if not group:
            _fail(action, FailureType.PEER_INVALID.value, "目标群不存在")
            return True
        link = session.scalar(
            select(TgGroupAccount).where(TgGroupAccount.group_id == group.id, TgGroupAccount.account_id == account.id, TgGroupAccount.can_send.is_(True))
        )
        if not link:
            _fail(action, FailureType.ACCOUNT_UNAVAILABLE.value, "该账号不可向此群发送")
            return True
        failure_type, failure_detail = validate_group_send_policy(session, tenant_id=action.tenant_id, group=group, content=content, review_approved=payload.review_approved)
        if failure_type:
            _fail(action, failure_type, failure_detail or failure_type)
            return True
        account_id = account.id
        group_peer = group.tg_peer_id
        group_pk = group.id
        session_ciphertext = account.session_ciphertext
        action.status = "executing"
        session.commit()
        result = gateway.send_message(
            account_id,
            group_pk,
            content,
            [OutboundSegment(segment_type="文本", content=content)],
            session_ciphertext,
            group_peer,
            credentials,
        )
        _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "")
        if result.ok:
            link.last_sent_at = _now()
        return True
    target_peer = payload.chat_id
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    action.status = "executing"
    session.commit()
    result = gateway.send_message_to_target(account_id, target_peer, content, "channel", None, session_ciphertext, credentials)
    _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "")
    return True


def _dispatch_view(action: Action, account: TgAccount, credentials, session: Session, payload: ViewMessagePayload) -> bool:
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    action.status = "executing"
    session.commit()
    result = gateway.view_channel_message(account_id, channel_peer, message_id, session_ciphertext, credentials)
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail)
    return True


def _dispatch_like(action: Action, account: TgAccount, credentials, session: Session, payload: LikeMessagePayload) -> bool:
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    reaction = payload.reaction_emoji
    action.status = "executing"
    session.commit()
    result = gateway.send_channel_reaction(account_id, channel_peer, message_id, reaction, session_ciphertext, credentials)
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail)
    return True


def _dispatch_comment(action: Action, account: TgAccount, credentials, session: Session, payload: PostCommentPayload) -> bool:
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    content = payload.comment_text
    action.status = "executing"
    session.commit()
    result = gateway.reply_channel_message(account_id, channel_peer, message_id, content, session_ciphertext, credentials)
    _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "")
    return True


def _apply_operation_result(action: Action, account: TgAccount, ok: bool, failure_type: str = "", detail: str = "") -> None:
    _apply_send_result(action, account, ok, "", failure_type, detail)


def _apply_send_result(action: Action, account: TgAccount, ok: bool, remote_id: str = "", failure_type: str = "", detail: str = "") -> None:
    if ok:
        action.status = "success"
        action.result = {"success": True, "telegram_msg_id": remote_id}
        account.last_active_at = _now()
    else:
        _fail(action, failure_type or FailureType.UNKNOWN.value, detail or "执行失败")
        if failure_type == FailureType.ACCOUNT_LIMITED.value:
            account.status = AccountStatus.LIMITED.value
            account.health_score = min(account.health_score, 55)
    action.executed_at = _now()


def _fail(action: Action, failure_type: str, detail: str) -> None:
    action.status = "failed"
    action.result = {"success": False, "error_code": failure_type, "error_message": detail}
    action.executed_at = _now()


__all__ = ["dispatch_action", "due_actions"]
