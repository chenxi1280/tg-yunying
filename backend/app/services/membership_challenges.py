from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.integrations.telegram import OperationResult
from app.integrations.telegram.search_join import (
    ImageVerificationConsensusUnavailableError,
    ImageVerificationDecision,
    ImageVerificationRequest,
    ImageVerificationVote,
)
from app.models import (
    AiProvider,
    AiProviderHealthStatus,
    TargetMembershipChallengeAttempt,
    TgAccount,
    VerificationTask,
)

from ._common import _now, ai_gateway, gateway
from .ai_config import ai_provider_credentials
from . import image_verification_ocr

MIN_IMAGE_VERIFICATION_CONFIDENCE = 0.70
TESSERACT_MIN_CONFIDENCE = 0.40
RAPIDOCR_MIN_CONFIDENCE = 0.50
IMAGE_VERIFICATION_PROVIDER_SOURCES = ("mimo", "minimax")
MIMO_V25_MODEL_MARKERS = ("mimo-v2.5", "mino-v2.5")
MINIMAX_M3_MODEL_MARKERS = ("minimax-m3", "minimax m3")
CN_NUMBER_CHARS = "零〇一二两三四五六七八九十"
ARITHMETIC_PATTERN = re.compile(rf"(?P<left>\d{{1,3}}|[{CN_NUMBER_CHARS}]{{1,4}})\s*(?P<op>[+\-＋－]|加|减)\s*(?P<right>\d{{1,3}}|[{CN_NUMBER_CHARS}]{{1,4}})")
CODE_PATTERN = re.compile(r"(?:验证码|code|captcha|请输入)[^\d]{0,16}(?P<code>\d{3,8})", re.IGNORECASE)
CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}

# PRD §2.19.2: 候选只在服务端校验，不进入模型 prompt。
SEARCH_JOIN_IMAGE_VERIFICATION_PROMPT = (
    "这是数学题，都是全数字，你来给出答案。"
    "answer 只能填写计算后的最终整数数字，不能填写算式、字母或其他字符。"
    "只输出紧凑 JSON：{\"answer\":\"答案整数\",\"confidence\":0到1}，不要解释。"
)
SEARCH_JOIN_STRING_VERIFICATION_PROMPT = (
    "这是是一段数字+字符的字符串你来告诉我结果。"
    "answer 只能填写图片中的最终数字+字符字符串，不能解释。"
    "只输出紧凑 JSON：{\"answer\":\"结果字符串\",\"confidence\":0到1}，不要解释。"
)
MATH_CHALLENGE_TEXT_MARKERS = ("计算结果", "数学题", "算式")
OCR_ARITHMETIC_PATTERN = re.compile(
    r"(?P<left>-?\d{1,4})(?P<op>[+\-*/])(?P<right>-?\d{1,4})"
)
SearchJoinImageVerificationSolver = Callable[
    [ImageVerificationRequest],
    "ImageVerificationDecision | None",
]


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
        detail = _missing_image_detail(context)
        return _image_verification_failure(session, task, account, detail, context=context)
    if _already_tried_image(session, task, image_message):
        return _image_verification_failure(session, task, account, "同一图片验证码已自动尝试过，需人工确认或等待新验证码", image_message, context)
    provider = _image_verification_provider(session)
    if provider is None:
        detail = "未配置可用多模态视觉供应商（MiMo/Mino 或 MiniMax）"
        return _image_verification_failure(session, task, account, detail, image_message, context)
    answer_source = _image_verification_provider_source(provider)
    provider_label = _image_verification_provider_label(provider, answer_source)
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
        detail = f"{provider_label} 图片验证码识别失败：{exc}"
        return _image_verification_failure(session, task, account, detail, image_message, context)
    if answer.confidence < MIN_IMAGE_VERIFICATION_CONFIDENCE:
        detail = f"{provider_label} 图片验证码识别低置信：{answer.confidence:.2f}"
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
        answer_source=answer_source,
        confidence=answer.confidence,
        model_name=provider.model_name,
        status=status,
        result_detail=result.detail or result.failure_type,
    )
    session.flush()
    if not result.ok:
        return OperationResult(False, "需人工处理", result.failure_type or "verification_send_failed", result.detail)
    detail = f"{provider_label} 已识别并提交验证码，置信度 {answer.confidence:.2f}"
    return ImageVerificationOperationResult(
        True,
        "已处理",
        detail=detail,
        attempt_context=context,
        image_message=image_message,
        answer_text=answer.answer,
        answer_source=answer_source,
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
            answer_source=answer_source or ("ai_image" if image_message and answer_text else ""),
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


def _missing_image_detail(context: dict[str, Any]) -> str:
    detail = str(context.get("read_failure_detail") or "未读取到验证码图片")
    messages = [message for message in context.get("messages") or [] if isinstance(message, dict)]
    media_count = sum(1 for message in messages if message.get("has_media"))
    status = str(context.get("context_status") or "unknown")
    return f"{detail}（context_status={status}, messages={len(messages)}, media={media_count}）"


def _image_verification_provider(session: Session) -> AiProvider | None:
    return next(iter(_image_verification_providers(session)), None)


def _image_verification_providers(session: Session) -> list[AiProvider]:
    providers = session.scalars(
        select(AiProvider).where(
            AiProvider.is_active.is_(True),
            AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value,
        ).order_by(AiProvider.id.asc())
    )
    return [provider for provider in providers if _image_verification_provider_source(provider)]


def _image_verification_provider_source(provider: AiProvider) -> str:
    text = " ".join([provider.provider_name or "", provider.model_name or "", provider.base_url or ""]).lower()
    if "xiaomimimo" in text or "xiaomimino" in text:
        return "mimo"
    tokens = {token for token in re.split(r"[^a-z0-9]+", text) if token}
    if tokens & {"mimo", "mino"}:
        return "mimo"
    return "minimax" if "minimax" in text else ""


def _image_verification_provider_label(provider: AiProvider, source: str) -> str:
    if source == "mimo":
        return "MiMo"
    if source == "minimax":
        return "MiniMax"
    return provider.provider_name or provider.model_name or "AI"


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
                TargetMembershipChallengeAttempt.answer_source.in_(IMAGE_VERIFICATION_PROVIDER_SOURCES),
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
    record_challenge_attempt(
        session,
        task,
        account,
        context or {},
        image_message=image_message,
        status="manual_required",
        result_detail=detail,
    )
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


def build_search_join_image_verification_solver(
    session: Session,
) -> SearchJoinImageVerificationSolver | None:
    """构造一个模型加两个独立 OCR 引擎的 2/3 共识 solver。"""
    providers = sorted(_image_verification_providers(session), key=_image_verification_provider_priority)
    if not providers:
        return None
    credentials = ai_provider_credentials(providers[0])

    def solver(
        request: ImageVerificationRequest,
    ) -> ImageVerificationDecision | None:
        if not request.image_bytes:
            return None
        votes = _collect_search_join_image_votes(credentials, request)
        return _consensus_decision(votes)

    return solver


def _collect_search_join_image_votes(
    credentials: Any,
    request: ImageVerificationRequest,
) -> tuple[ImageVerificationVote, ...]:
    recognizers = (
        (
            _image_provider_label(credentials) if credentials else "multimodal",
            lambda: _model_recognition(credentials, request),
            MIN_IMAGE_VERIFICATION_CONFIDENCE,
        ),
        (
            "tesseract",
            lambda: image_verification_ocr.recognize_with_tesseract(request.image_bytes),
            TESSERACT_MIN_CONFIDENCE,
        ),
        (
            "rapidocr",
            lambda: image_verification_ocr.recognize_with_rapidocr(request.image_bytes),
            RAPIDOCR_MIN_CONFIDENCE,
        ),
    )
    with ThreadPoolExecutor(max_workers=len(recognizers)) as executor:
        futures = [
            executor.submit(_recognition_vote, source, recognize, threshold, request)
            for source, recognize, threshold in recognizers
        ]
        return tuple(future.result() for future in futures)


def _model_recognition(
    credentials: Any,
    request: ImageVerificationRequest,
) -> tuple[str, float]:
    if credentials is None:
        raise RuntimeError("no healthy approved multimodal provider")
    return _solve_search_join_image(
        credentials,
        request.image_bytes,
        request.mime_type,
        request.challenge_text,
    )


def _recognition_vote(
    source: str,
    recognize: Callable[[], tuple[str, float]],
    confidence_threshold: float,
    request: ImageVerificationRequest,
) -> ImageVerificationVote:
    try:
        raw_answer, confidence = recognize()
    except Exception as exc:  # noqa: BLE001 - each source must expose its own failure.
        detail = exc.__class__.__name__
        return ImageVerificationVote(source, "unavailable", detail=detail)
    answer = _normalize_recognition_answer(raw_answer, request)
    in_candidates = answer in frozenset(request.candidate_answers)
    if confidence < confidence_threshold:
        return ImageVerificationVote(
            source, "low_confidence", answer, confidence, in_candidates
        )
    status = "accepted" if answer and in_candidates else "unsafe"
    return ImageVerificationVote(
        source, status, answer, confidence, in_candidates
    )


def _consensus_decision(
    votes: tuple[ImageVerificationVote, ...],
) -> ImageVerificationDecision:
    accepted = [vote for vote in votes if vote.status == "accepted"]
    counts = Counter(vote.answer for vote in accepted)
    if counts:
        answer, count = counts.most_common(1)[0]
        if count >= 2:
            return ImageVerificationDecision(
                answer=answer,
                confidence=count / len(votes),
                votes=votes,
            )
    detail = "; ".join(
        f"{vote.source}:{vote.status}:in_candidates={str(vote.in_candidates).lower()}"
        for vote in votes
    )
    raise ImageVerificationConsensusUnavailableError(detail, votes)


def _normalize_recognition_answer(
    raw_answer: object,
    request: ImageVerificationRequest,
) -> str:
    value = unicodedata.normalize("NFKC", str(raw_answer or ""))
    if _is_math_challenge(request):
        return _normalize_math_answer(value)
    return "".join(char for char in value if char.isascii() and char.isalnum())


def _is_math_challenge(request: ImageVerificationRequest) -> bool:
    if any(marker in request.challenge_text for marker in MATH_CHALLENGE_TEXT_MARKERS):
        return True
    return bool(request.candidate_answers) and all(
        answer.isdigit() for answer in request.candidate_answers
    )


def _normalize_math_answer(value: str) -> str:
    compact = re.sub(r"\s+", "", value).replace("×", "*").replace("÷", "/")
    compact = compact.replace("x", "*").replace("X", "*")
    match = OCR_ARITHMETIC_PATTERN.search(compact)
    if match is None:
        direct = re.fullmatch(r"[=?]*(-?\d+)[=?]*", compact)
        return direct.group(1) if direct else ""
    left = int(match.group("left"))
    right = int(match.group("right"))
    return _calculate_integer(left, match.group("op"), right)


def _calculate_integer(left: int, operation: str, right: int) -> str:
    if operation == "+":
        return str(left + right)
    if operation == "-":
        return str(left - right)
    if operation == "*":
        return str(left * right)
    if right and left % right == 0:
        return str(left // right)
    return ""


def _image_verification_provider_priority(provider: AiProvider) -> tuple[int, int]:
    model = str(getattr(provider, "model_name", "") or "").strip().lower()
    if any(marker in model for marker in MIMO_V25_MODEL_MARKERS):
        priority = 0
    elif any(marker in model for marker in MINIMAX_M3_MODEL_MARKERS):
        priority = 1
    else:
        priority = 2
    return priority, int(provider.id)


def _solve_search_join_image(
    credentials: Any,
    image_bytes: bytes,
    mime_type: str,
    challenge_text: str,
) -> tuple[str, float]:
    prompt = _search_join_image_verification_prompt(challenge_text)
    return _solve_search_join_image_once(
        credentials,
        image_bytes,
        mime_type,
        prompt,
    )


def _solve_search_join_image_once(
    credentials: Any,
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
) -> tuple[str, float]:
    result = ai_gateway.solve_image_verification(
        credentials,
        image_bytes,
        mime_type or "image/png",
        prompt=prompt,
    )
    answer = str(result.answer or "").strip()
    if not answer:
        return "", float(result.confidence or 0.0)
    return answer, float(result.confidence or 0.0)


def _search_join_image_verification_prompt(challenge_text: str) -> str:
    if any(
        marker in challenge_text
        for marker in MATH_CHALLENGE_TEXT_MARKERS
    ):
        return SEARCH_JOIN_IMAGE_VERIFICATION_PROMPT
    return SEARCH_JOIN_STRING_VERIFICATION_PROMPT


def _image_provider_label(credentials: Any) -> str:
    provider = str(getattr(credentials, "provider_name", "") or "AI")
    model = str(getattr(credentials, "model_name", "") or "unknown")
    return f"{provider}({model})"
