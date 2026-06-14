from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.telegram import OperationResult
from app.models import (
    AiProvider,
    AiProviderHealthStatus,
    TargetMembershipChallengeAttempt,
    TgAccount,
    VerificationTask,
)

from ._common import _now, ai_gateway, gateway
from .ai_config import ai_provider_credentials

MIN_IMAGE_VERIFICATION_CONFIDENCE = 0.70
CN_NUMBER_CHARS = "零〇一二两三四五六七八九十"
ARITHMETIC_PATTERN = re.compile(rf"(?P<left>\d{{1,3}}|[{CN_NUMBER_CHARS}]{{1,4}})\s*(?P<op>[+\-＋－]|加|减)\s*(?P<right>\d{{1,3}}|[{CN_NUMBER_CHARS}]{{1,4}})")
CODE_PATTERN = re.compile(r"(?:验证码|code|captcha|请输入)[^\d]{0,16}(?P<code>\d{3,8})", re.IGNORECASE)
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


@dataclass(frozen=True)
class ImageVerificationOperationResult(OperationResult):
    attempt_context: dict[str, Any] | None = None
    image_message: dict[str, Any] | None = None
    answer_text: str = ""
    answer_source: str = ""
    confidence: float = 0.0
    model_name: str = ""


@dataclass(frozen=True)
class ChallengeContextReadResult:
    context: dict[str, Any]
    reader_account: TgAccount
    reader_credentials: Any


def read_challenge_context(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    credentials: Any,
    *,
    submit_account: TgAccount | None = None,
) -> dict[str, Any]:
    try:
        messages = gateway.fetch_verification_context(
            account.id,
            task.target_peer_id,
            account.session_ciphertext,
            credentials,
        )
        status, detail = _context_status(messages)
    except Exception as exc:  # noqa: BLE001 - operator-facing diagnostic state.
        messages = []
        status = "read_failed"
        detail = str(exc) or exc.__class__.__name__
    context = _context_payload(task, account, messages, status, detail, submit_account=submit_account)
    record_challenge_attempt(session, task, account, context, status="context_read")
    if status in {"empty", "read_failed", "target_inaccessible"}:
        task.status = "需人工处理"
        task.failure_detail = detail
    return context


def read_challenge_context_with_fallback(
    session: Session,
    task: VerificationTask,
    submit_account: TgAccount,
    submit_credentials: Any,
    reader_candidates: list[tuple[TgAccount, Any]] | None = None,
) -> ChallengeContextReadResult:
    primary = read_challenge_context(session, task, submit_account, submit_credentials, submit_account=submit_account)
    if primary["context_status"] == "ok":
        return ChallengeContextReadResult(primary, submit_account, submit_credentials)
    last = ChallengeContextReadResult(primary, submit_account, submit_credentials)
    for reader, reader_credentials in reader_candidates or []:
        if reader.id == submit_account.id:
            continue
        context = read_challenge_context(session, task, reader, reader_credentials, submit_account=submit_account)
        last = ChallengeContextReadResult(context, reader, reader_credentials)
        if context["context_status"] == "ok":
            task.status = "需人工处理"
            task.failure_detail = f"已由读取账号 #{reader.id} 读取验证上下文，等待加入账号提交验证。"
            return last
    return last


def auto_resolve_image_verification(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    credentials: Any,
    *,
    reader_candidates: list[tuple[TgAccount, Any]] | None = None,
) -> OperationResult:
    read_result = read_challenge_context_with_fallback(
        session,
        task,
        account,
        credentials,
        reader_candidates=reader_candidates,
    )
    context = read_result.context
    image_message = _latest_context_image(context["messages"])
    if not image_message:
        text_result = _submit_text_answer_from_context(session, task, account, credentials, context)
        if text_result:
            return text_result
        detail = context.get("read_failure_detail") or "未读取到验证码图片"
        return _image_verification_failure(session, task, account, detail, context=context)
    if _already_tried_image(session, task, image_message):
        return _image_verification_failure(session, task, account, "同一图片验证码已自动尝试过，需人工确认或等待新验证码", image_message, context)
    provider = _mimo_vision_provider(session)
    if provider is None:
        return _image_verification_failure(session, task, account, "未配置可用 MiMo 视觉供应商", image_message, context)
    media = gateway.fetch_verification_media(
        read_result.reader_account.id,
        task.target_peer_id,
        int(image_message["media_message_id"]),
        read_result.reader_account.session_ciphertext,
        read_result.reader_credentials,
    )
    if not media.ok:
        detail = media.detail or media.failure_type or "验证码图片下载失败"
        return _image_verification_failure(session, task, account, detail, image_message, context)
    try:
        answer = ai_gateway.solve_image_verification(
            ai_provider_credentials(provider),
            media.data,
            media.detail or image_message.get("media_mime_type") or "image/png",
        )
    except Exception as exc:  # noqa: BLE001 - stored as explicit operator-facing attempt.
        detail = f"MiMo 图片验证码识别失败：{exc}"
        return _image_verification_failure(session, task, account, detail, image_message, context)
    if answer.confidence < MIN_IMAGE_VERIFICATION_CONFIDENCE:
        detail = f"MiMo 图片验证码识别低置信：{answer.confidence:.2f}"
        return _image_verification_failure(session, task, account, detail, image_message, context)
    result = gateway.submit_verification_response(account.id, task.target_peer_id, answer.answer, account.session_ciphertext, credentials)
    status = "sent" if result.ok else "send_failed"
    record_challenge_attempt(
        session,
        task,
        account,
        context,
        image_message=image_message,
        answer_text=answer.answer,
        answer_source="mimo",
        confidence=answer.confidence,
        model_name=provider.model_name,
        status=status,
        result_detail=result.detail or result.failure_type,
    )
    session.flush()
    if not result.ok:
        return OperationResult(False, "需人工处理", result.failure_type or "verification_send_failed", result.detail)
    detail = f"MiMo 已识别并提交验证码，置信度 {answer.confidence:.2f}"
    return ImageVerificationOperationResult(
        True,
        "已处理",
        detail=detail,
        attempt_context=context,
        image_message=image_message,
        answer_text=answer.answer,
        answer_source="mimo",
        confidence=answer.confidence,
        model_name=provider.model_name,
    )


def auto_resolve_text_verification(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    credentials: Any,
    *,
    reader_candidates: list[tuple[TgAccount, Any]] | None = None,
) -> OperationResult:
    read_result = read_challenge_context_with_fallback(
        session,
        task,
        account,
        credentials,
        reader_candidates=reader_candidates,
    )
    answer = _text_verification_answer(task, read_result.context)
    if not answer:
        detail = read_result.context.get("read_failure_detail") or "未从验证上下文识别到可提交的文本答案"
        task.status = "需人工处理"
        task.failure_detail = str(detail)
        record_challenge_attempt(session, task, account, read_result.context, status="text_answer_missing")
        return OperationResult(False, "需人工处理", "verification_answer_missing", str(detail))
    result = gateway.submit_verification_response(
        account.id,
        task.target_peer_id,
        answer,
        account.session_ciphertext,
        credentials,
    )
    record_challenge_attempt(
        session,
        task,
        account,
        read_result.context,
        answer_text=answer,
        answer_source="rule",
        challenge_type=_text_challenge_type(read_result.context),
        status="text_answer_sent" if result.ok else "text_answer_send_failed",
        result_detail=result.detail or result.failure_type,
    )
    if not result.ok:
        return OperationResult(False, "需人工处理", result.failure_type or "verification_send_failed", result.detail)
    return OperationResult(True, "已处理", detail=result.detail or "文本验证码已提交")


def _submit_text_answer_from_context(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    credentials: Any,
    context: dict[str, Any],
) -> ImageVerificationOperationResult | None:
    answer = _text_verification_answer(task, context)
    if not answer:
        return None
    result = gateway.submit_verification_response(account.id, task.target_peer_id, answer, account.session_ciphertext, credentials)
    status = "text_answer_sent" if result.ok else "text_answer_send_failed"
    detail = result.detail or result.failure_type or "文本验证码已提交"
    record_challenge_attempt(
        session,
        task,
        account,
        context,
        answer_text=answer,
        answer_source="rule",
        challenge_type=_text_challenge_type(context),
        status=status,
        result_detail=detail,
    )
    session.flush()
    if not result.ok:
        return ImageVerificationOperationResult(
            False,
            "需人工处理",
            result.failure_type or "verification_send_failed",
            detail,
            attempt_context=context,
            answer_text=answer,
            answer_source="rule",
        )
    return ImageVerificationOperationResult(
        True,
        "已处理",
        detail=detail,
        attempt_context=context,
        answer_text=answer,
        answer_source="rule",
    )


def record_challenge_attempt(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    context: dict[str, Any],
    *,
    image_message: dict[str, Any] | None = None,
    answer_text: str = "",
    confidence: float = 0.0,
    model_name: str = "",
    answer_source: str = "",
    challenge_type: str = "",
    status: str,
    result_detail: str = "",
) -> None:
    image_message = image_message or {}
    session.add(
        TargetMembershipChallengeAttempt(
            tenant_id=task.tenant_id,
            verification_task_id=task.id,
            account_id=account.id,
            group_id=task.group_id,
            challenge_type=challenge_type or ("image_captcha" if image_message else "context"),
            question_hash=_question_hash(task, image_message),
            question_snapshot=task.detected_reason or "",
            context_status=str(context.get("context_status") or ""),
            context_message_count=int(context.get("message_count") or 0),
            context_failure_detail=str(context.get("read_failure_detail") or result_detail or ""),
            media_message_id=str(image_message.get("media_message_id") or ""),
            media_fingerprint=str(image_message.get("media_fingerprint") or ""),
            media_mime_type=str(image_message.get("media_mime_type") or ""),
            answer_source=answer_source or ("mimo" if answer_text else ""),
            answer_text=answer_text,
            confidence=confidence,
            model_name=model_name,
            status=status,
            result_snapshot=json.dumps({"detail": result_detail}, ensure_ascii=False),
            created_by="system",
        )
    )


def _context_payload(
    task: VerificationTask,
    reader_account: TgAccount,
    messages: list[dict[str, Any]],
    context_status: str,
    detail: str,
    *,
    submit_account: TgAccount | None = None,
) -> dict[str, Any]:
    submitter = submit_account or reader_account
    return {
        "task_id": task.id,
        "account_id": reader_account.id,
        "submit_account_id": submitter.id,
        "reader_account_id": reader_account.id,
        "target_display": task.target_display,
        "target_peer_id": task.target_peer_id,
        "detected_reason": task.detected_reason,
        "failure_detail": task.failure_detail,
        "suggested_action": task.suggested_action,
        "context_status": context_status,
        "last_read_at": _now(),
        "message_count": len(messages),
        "read_failure_detail": detail,
        "messages": messages,
    }


def _text_verification_answer(task: VerificationTask, context: dict[str, Any]) -> str:
    texts = [task.detected_reason or "", task.failure_detail or ""]
    texts.extend(str(message.get("text") or "") for message in context.get("messages") or [] if isinstance(message, dict))
    combined = "\n".join(text for text in texts if text)
    return _arithmetic_answer(combined) or _code_answer(combined)


def _arithmetic_answer(text: str) -> str:
    match = ARITHMETIC_PATTERN.search(text)
    if not match:
        return ""
    left = _number_value(match.group("left"))
    right = _number_value(match.group("right"))
    if left is None or right is None:
        return ""
    result = left + right if match.group("op") in {"+", "＋", "加"} else left - right
    return str(result) if 0 <= result <= 9999 else ""


def _number_value(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = CN_DIGITS.get(left, 1 if left == "" else None)
        ones = CN_DIGITS.get(right, 0 if right == "" else None)
        return tens * 10 + ones if tens is not None and ones is not None else None
    if len(raw) == 1:
        return CN_DIGITS.get(raw)
    return None


def _code_answer(text: str) -> str:
    match = CODE_PATTERN.search(text)
    return match.group("code") if match else ""


def _text_challenge_type(context: dict[str, Any]) -> str:
    texts = " ".join(str(message.get("text") or "") for message in context.get("messages") or [] if isinstance(message, dict))
    return "arithmetic_captcha" if _arithmetic_answer(texts) else "text_captcha"


def _context_status(messages: list[dict[str, Any]]) -> tuple[str, str]:
    if messages:
        return "ok", ""
    detail = "没有读取到最近验证聊天信息。请确认验证消息是否仍存在、账号是否能读取群历史。"
    return "empty", detail


def _mimo_vision_provider(session: Session) -> AiProvider | None:
    providers = session.scalars(
        select(AiProvider).where(
            AiProvider.is_active.is_(True),
            AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value,
        )
    )
    return next((provider for provider in providers if _looks_like_mimo_provider(provider)), None)


def _looks_like_mimo_provider(provider: AiProvider) -> bool:
    text = " ".join([provider.provider_name or "", provider.model_name or "", provider.base_url or ""]).lower()
    if "xiaomimimo" in text or "xiaomimino" in text:
        return True
    tokens = {token for token in re.split(r"[^a-z0-9]+", text) if token}
    return bool(tokens & {"mimo", "mino"})


def _latest_context_image(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((message for message in messages if message.get("has_media") and message.get("media_message_id")), None)


def _already_tried_image(session: Session, task: VerificationTask, image_message: dict[str, Any]) -> bool:
    fingerprint = str(image_message.get("media_fingerprint") or "")
    if not fingerprint:
        return False
    return bool(
        session.scalar(
            select(TargetMembershipChallengeAttempt.id)
            .where(
                TargetMembershipChallengeAttempt.verification_task_id == task.id,
                TargetMembershipChallengeAttempt.media_fingerprint == fingerprint,
                TargetMembershipChallengeAttempt.answer_source == "mimo",
            )
            .limit(1)
        )
    )


def _image_verification_failure(
    session: Session,
    task: VerificationTask,
    account: TgAccount,
    detail: str,
    image_message: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> ImageVerificationOperationResult:
    record_challenge_attempt(session, task, account, context or {}, image_message=image_message, status="manual_required", result_detail=detail)
    session.flush()
    return ImageVerificationOperationResult(
        False,
        "需人工处理",
        "image_verification_manual_required",
        detail,
        attempt_context=context,
        image_message=image_message,
    )


def _question_hash(task: VerificationTask, image_message: dict[str, Any]) -> str:
    raw = "|".join([str(task.id), task.detected_reason or "", str(image_message.get("media_fingerprint") or "")])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
