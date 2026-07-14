from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, GroupContextMessage, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now

from .ai_generation_dependencies import GenerationDependencies
from .ai_generation_commit import commit_generation_action, load_generation_batch
from .ai_generation_persistence import persist_generation_results as _persist_generation_results
from .ai_generation_state import (
    GenerationAttemptStale,
    GenerationMappingError,
    begin_generation_attempt,
    cached_generation_result,
)
from .ai_generation_recovery import persist_generation_unknown
from .ai_generation_pipeline import SlotGenerationResult, generate_quality_results
from .ai_generator import AI_GENERATION_UNAVAILABLE_MESSAGE, AiGenerationUnavailable
from .ai_generation_quality import fail_generation_action, fail_generation_batch
from .payloads import SendMessagePayload


GENERATION_BATCH_SIZE = 10
GENERATION_LOOKAHEAD_SECONDS = 120
CONTEXT_HISTORY_LIMIT = 50
CONTEXT_HISTORY_MAX_CHARS = 1000


@dataclass(frozen=True)
class GenerationRequest:
    action_id: str
    tenant_id: int
    task_id: str
    group_id: int
    account_id: int
    session_ciphertext: str
    credentials: object
    peer_id: str
    is_reply: bool
    target_label: str
    history: str
    config: dict
    reply_targets: list[dict]
    batch_ids: list[str]
    attempt_id: str
    claim_owner: str
    claim_token: str
    cached_contents: list[str]
    cached_tokens: int
    duplicate_baseline_messages: list[str]
    quality_snapshots: list[dict]
    chat_mode: str
    context_message_ids: list[int]
    fact_anchor_required: bool
    low_confidence_silence_enabled: bool


def ensure_send_message_content(
    session: Session,
    action: Action,
    account: TgAccount,
    *,
    payload: SendMessagePayload,
    credentials=None,
    dependencies: GenerationDependencies,
) -> SendMessagePayload:
    if payload.message_text.strip():
        return payload
    if payload.ai_generation_status not in {"pending", "ai_result_persist_unknown"}:
        raise AiGenerationUnavailable("send_message action 缺少可发送文案")
    task = session.get(Task, action.task_id) if action.task_id else None
    if not task:
        raise AiGenerationUnavailable("AI 生成缺少任务配置")
    batch = _pending_generation_batch(session, action, payload)
    batch = _refresh_normal_context(session, task, batch)
    request = _prepare_generation_request(
        session,
        task,
        batch,
        account=account,
        credentials=credentials,
    )
    results, tokens = _generate_request_results(session, request, dependencies)
    _commit_generation_results(session, request, results, tokens=tokens)
    refreshed_action = session.get(Action, action.id)
    refreshed = SendMessagePayload.model_validate(refreshed_action.payload or {})
    if refreshed_action.status == "failed" or refreshed.ai_generation_status != "ready":
        raise AiGenerationUnavailable(refreshed.ai_generation_status or AI_GENERATION_UNAVAILABLE_MESSAGE)
    if not refreshed.message_text.strip():
        raise AiGenerationUnavailable(AI_GENERATION_UNAVAILABLE_MESSAGE)
    return refreshed


def _generate_request_results(
    session: Session,
    request: GenerationRequest,
    dependencies: GenerationDependencies,
) -> tuple[list[SlotGenerationResult], int]:
    try:
        return _generate_without_transaction(session, request, dependencies)
    except GenerationMappingError as exc:
        fail_generation_batch(session, request, str(exc), detail=str(exc))
        session.commit()
        raise AiGenerationUnavailable(str(exc)) from exc
    except GenerationAttemptStale:
        session.rollback()
        raise


def _commit_generation_results(
    session: Session,
    request: GenerationRequest,
    results: list[SlotGenerationResult],
    *,
    tokens: int,
) -> None:
    try:
        _persist_generation_results(session, request, results, tokens=tokens)
        session.commit()
    except GenerationMappingError as exc:
        session.rollback()
        fail_generation_batch(
            session,
            request,
            str(exc),
            detail=str(exc),
        )
        session.commit()
        raise AiGenerationUnavailable(str(exc)) from exc
    except GenerationAttemptStale:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        contents = [result.content for result in results]
        persist_generation_unknown(
            session,
            request,
            contents,
            tokens=tokens,
            attempt_id=request.attempt_id,
        )
        session.commit()
        raise AiGenerationUnavailable("ai_result_persist_unknown")


def _prepare_generation_request(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
    *,
    account: TgAccount,
    credentials,
) -> GenerationRequest:
    action, payload = batch[0]
    peer_id = _validate_local_reply_target(
        session,
        task,
        action,
        payload=payload,
        account_id=account.id,
    )
    cached = [cached_generation_result(item) for _row, item in batch]
    cached_contents = [item[0] for item in cached if item]
    cached_tokens = sum(item[1] for item in cached if item)
    attempt_id = payload.ai_generation_attempt_id
    if len(cached_contents) != len(batch):
        attempt_id, _request_id = begin_generation_attempt(batch, _now())
        cached_contents = []
        cached_tokens = 0
    request = _generation_request(
        task,
        batch,
        account,
        credentials=credentials,
        peer_id=peer_id,
        attempt_id=attempt_id,
    )
    session.commit()
    return GenerationRequest(
        **{**request.__dict__, "cached_contents": cached_contents, "cached_tokens": cached_tokens},
    )


def _generation_request(
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
    account: TgAccount,
    *,
    credentials,
    peer_id: str,
    attempt_id: str,
) -> GenerationRequest:
    action, payload = batch[0]
    return GenerationRequest(
        action_id=action.id,
        tenant_id=action.tenant_id,
        task_id=str(action.task_id or ""),
        group_id=int(payload.group_id or 0),
        account_id=account.id,
        session_ciphertext=account.session_ciphertext,
        credentials=credentials,
        peer_id=peer_id,
        is_reply=bool(payload.reply_to_message_id),
        target_label=payload.target_display,
        history=payload.ai_generation_history,
        config={**_runtime_config(task, batch), "_close_db_transaction_before_ai": True},
        reply_targets=_reply_targets(batch),
        batch_ids=[row.id for row, _item in batch],
        attempt_id=attempt_id,
        claim_owner=payload.ai_generation_claim_owner,
        claim_token=payload.ai_generation_claim_token,
        cached_contents=[],
        cached_tokens=0,
        duplicate_baseline_messages=[line for line in payload.ai_generation_history.splitlines() if line.strip()],
        quality_snapshots=[_quality_snapshot(item) for _row, item in batch],
        chat_mode="reply" if payload.reply_to_message_id else ("idle_warmup" if payload.ai_generation_history else "bootstrap"),
        context_message_ids=list(payload.context_message_ids),
        fact_anchor_required=bool((task.type_config or {}).get("fact_anchor_required", True)),
        low_confidence_silence_enabled=bool((task.type_config or {}).get("low_confidence_silence_enabled", True)),
    )


def _generate_without_transaction(
    session: Session,
    request: GenerationRequest,
    dependencies: GenerationDependencies,
) -> tuple[list[SlotGenerationResult], int]:
    if request.is_reply:
        _validate_remote_reply_target(session, request, dependencies)
    if not request.cached_contents:
        _mark_provider_call_started(session, request)
    try:
        return generate_quality_results(session, request, dependencies)
    except AiGenerationUnavailable:
        fail_generation_batch(
            session,
            request,
            "ai_generation_failed",
            detail=AI_GENERATION_UNAVAILABLE_MESSAGE,
        )
        session.commit()
        raise


def _mark_provider_call_started(session: Session, request: GenerationRequest) -> None:
    timestamp = _now().isoformat()
    for action, _payload in load_generation_batch(session, request):
        action.result = {
            **(action.result or {}),
            "generation_stage": "provider_call_started",
            "ai_provider_call_started_at": timestamp,
        }
        commit_generation_action(session, request, action)
    session.commit()


def _validate_local_reply_target(
    session: Session,
    task: Task,
    action: Action,
    *,
    payload: SendMessagePayload,
    account_id: int,
) -> str:
    if not payload.reply_to_message_id:
        return ""
    group = session.scalar(select(TgGroup).where(
        TgGroup.tenant_id == action.tenant_id,
        TgGroup.id == payload.group_id,
    ))
    target = session.scalar(select(GroupContextMessage.id).where(
        GroupContextMessage.tenant_id == action.tenant_id,
        GroupContextMessage.group_id == payload.group_id,
        GroupContextMessage.remote_message_id == str(payload.reply_to_message_id),
    ))
    link = session.scalar(select(TgGroupAccount.id).where(
        TgGroupAccount.tenant_id == action.tenant_id,
        TgGroupAccount.group_id == payload.group_id,
        TgGroupAccount.account_id == account_id,
        TgGroupAccount.can_send.is_(True),
    ))
    window = int((task.type_config or {}).get("context_bound_schedule_window_seconds") or 300)
    stale = (_naive(_now()) - _naive(action.created_at)).total_seconds() > window
    if group and target and link and not stale:
        return group.tg_peer_id
    code = "reply_target_stale" if stale else "reply_target_missing"
    fail_generation_action(
        action,
        code,
        "引用目标已过期或当前账号不可引用",
        stage="ai_reply_target",
    )
    raise AiGenerationUnavailable(code)


def _validate_remote_reply_target(
    session: Session,
    request: GenerationRequest,
    dependencies: GenerationDependencies,
) -> None:
    probe = dependencies.reply_target_probe(
        request.account_id,
        request.peer_id,
        "group",
        request.session_ciphertext,
        request.credentials,
    )
    snapshots = []
    if probe.ok:
        snapshots = dependencies.reply_messages_fetcher(
            request.account_id,
            request.peer_id,
            request.session_ciphertext,
            request.credentials,
            limit=CONTEXT_HISTORY_LIMIT,
        )
    target_id = str(request.reply_targets[0]["message_id"])
    if probe.ok and any(str(item.remote_message_id) == target_id for item in snapshots):
        return
    action = session.get(Action, request.action_id)
    fail_generation_action(
        action,
        "reply_target_missing",
        probe.detail or "远端引用目标不存在或不可访问",
        stage="ai_reply_target",
    )
    session.commit()
    raise AiGenerationUnavailable("reply_target_missing")


def _pending_generation_batch(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
) -> list[tuple[Action, SendMessagePayload]]:
    rows = [(action, payload)]
    if payload.reply_to_message_id or not payload.ai_generation_id or not payload.ai_generation_claim_token:
        return rows
    siblings = session.scalars(_normal_sibling_query(action, payload))
    for sibling in siblings:
        rows.append((sibling, SendMessagePayload.model_validate(sibling.payload or {})))
    return rows


def _normal_sibling_query(action: Action, payload: SendMessagePayload):
    cutoff = max(_naive(action.scheduled_at), _naive(_now())) + timedelta(seconds=GENERATION_LOOKAHEAD_SECONDS)
    stmt = (
        select(Action)
        .where(
            Action.id != action.id,
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.action_type == "send_message",
            Action.status == "executing",
            Action.payload["ai_generation_claim_owner"].as_string() == payload.ai_generation_claim_owner,
            Action.payload["ai_generation_claim_token"].as_string() == payload.ai_generation_claim_token,
            Action.scheduled_at <= cutoff,
            Action.payload["ai_generation_status"].as_string() == "pending",
            Action.payload["ai_generation_id"].as_string() == payload.ai_generation_id,
            or_(
                Action.payload["reply_to_message_id"].as_integer().is_(None),
                Action.payload["reply_to_message_id"].as_integer() == 0,
            ),
        )
        .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
        .limit(GENERATION_BATCH_SIZE - 1)
    )
    if action._sa_instance_state.session.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update(skip_locked=True, of=Action)
    return stmt


def _refresh_normal_context(
    session: Session,
    task: Task,
    batch: list[tuple[Action, SendMessagePayload]],
) -> list[tuple[Action, SendMessagePayload]]:
    if not batch or batch[0][1].reply_to_message_id:
        return batch
    rows = _latest_context_rows(session, batch[0][1], task)
    if not rows:
        return batch
    history = "\n".join(f"{row.sender_name}: {row.content}" for row in rows)[-CONTEXT_HISTORY_MAX_CHARS:]
    context_ids = [int(row.id) for row in rows]
    refreshed = []
    for action, payload in batch:
        updated = payload.model_copy(update={
            "anchor_message_ids": context_ids,
            "context_message_ids": context_ids,
            "context_snapshot_message_id": max(context_ids),
            "ai_generation_history": history,
            "ai_generation_context_count": len(context_ids),
        })
        action.payload = updated.model_dump(mode="json")
        refreshed.append((action, updated))
    return refreshed


def _latest_context_rows(session: Session, payload: SendMessagePayload, task: Task) -> list[GroupContextMessage]:
    depth = min(CONTEXT_HISTORY_LIMIT, max(1, int((task.type_config or {}).get("chat_history_depth") or CONTEXT_HISTORY_LIMIT)))
    rows = session.scalars(
        select(GroupContextMessage)
        .where(
            GroupContextMessage.tenant_id == task.tenant_id,
            GroupContextMessage.group_id == payload.group_id,
            GroupContextMessage.is_bot.is_(False),
            GroupContextMessage.content != "",
        )
        .order_by(func.coalesce(GroupContextMessage.sent_at, GroupContextMessage.created_at).desc())
        .limit(depth)
    )
    return list(reversed(list(rows)))


def _runtime_config(task: Task, batch: list[tuple[Action, SendMessagePayload]]) -> dict:
    config = dict(task.type_config or {})
    config["account_personas"] = _payload_map(batch, "account_role")
    config["account_memories"] = _payload_map(batch, "account_memory")
    config["account_profiles"] = _payload_map(batch, "account_profile")
    config["generation_slots"] = [_generation_slot(row, item, index) for index, (row, item) in enumerate(batch, 1)]
    first = batch[0][1]
    if first.topic_thread:
        config["topic_thread"] = first.topic_thread
    if first.topic_plan:
        config["topic_plan"] = first.topic_plan
    return config


def _payload_map(batch: list[tuple[Action, SendMessagePayload]], attr: str) -> dict[str, str]:
    return {
        str(action.account_id): value
        for action, payload in batch
        if action.account_id and (value := str(getattr(payload, attr) or "").strip())
    }


def _generation_slot(action: Action, payload: SendMessagePayload, index: int) -> dict:
    return {
        "slot_id": payload.slot_id,
        "sequence_index": index,
        "cycle_turn_index": int(payload.turn_index or index),
        "account_id": action.account_id,
        "coverage_ledger_id": payload.coverage_ledger_id,
        "act_type": payload.act_type,
        "account_profile": payload.account_profile,
        "reply_to_message_id": payload.reply_to_message_id,
        "reply_to_content": payload.reply_target_preview,
        "reply_to_sequence_index": index if payload.reply_to_message_id else None,
        "topic_direction": dict(payload.topic_direction),
        "teacher_target": dict(payload.teacher_target),
    }


def _reply_targets(batch: list[tuple[Action, SendMessagePayload]]) -> list[dict]:
    return [{
        "message_id": int(payload.reply_to_message_id or 0),
        "author": payload.reply_target_author,
        "preview": payload.reply_target_preview,
        "source": payload.reply_target_source,
    } for _action, payload in batch]


def _quality_snapshot(payload: SendMessagePayload) -> dict:
    return {
        "account_profile": payload.account_profile,
        "stance_summary": payload.stance_summary,
    }


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


__all__ = ["GenerationAttemptStale", "ensure_send_message_content"]
