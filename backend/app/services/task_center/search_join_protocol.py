from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, SearchJoinProtocolTrace


HOT_LIST_RESET_KIND = "hot_list_reset"
INITIAL_TRACE_KIND = "initial"
RECENT_TRACE_LIMIT = 10


def record_search_join_protocol_trace(
    session: Session,
    action: Action,
    *,
    payload: dict,
    result: dict,
    attempt: ExecutionAttempt,
) -> SearchJoinProtocolTrace:
    recovery_kind = str(result.get("jisou_recovery_kind") or payload.get("jisou_recovery_kind") or INITIAL_TRACE_KIND)
    trace = _trace_for_update(session, action.id, recovery_kind)
    phase = str(result.get("jisou_page_phase") or "unknown_page")
    if trace is None:
        trace = SearchJoinProtocolTrace(
            tenant_id=action.tenant_id,
            task_id=action.task_id,
            action_id=action.id,
            bot_username=str(payload.get("bot_username") or "").lstrip("@"),
            protocol_sample_version=str(payload.get("protocol_sample_version") or ""),
            recovery_kind=recovery_kind,
            attempt_no=attempt.attempt_no,
            event_type=_trace_event_type(result, recovery_kind),
            page_phase=phase,
            status="observed",
            trace_summary=_safe_trace_summary(result),
        )
        session.add(trace)
        session.flush()
        return trace
    _update_reset_trace(trace, phase, result)
    return trace


def task_search_join_protocol_snapshot(session: Session, task_id: str) -> dict:
    traces = list(session.scalars(
        select(SearchJoinProtocolTrace)
        .where(SearchJoinProtocolTrace.task_id == task_id)
        .order_by(SearchJoinProtocolTrace.updated_at.desc(), SearchJoinProtocolTrace.id.desc())
        .limit(RECENT_TRACE_LIMIT)
    ))
    if not traces:
        return {}
    latest = traces[0]
    return {
        "latest_page_phase": latest.page_phase,
        "latest_protocol_sample_version": latest.protocol_sample_version,
        "recent_traces": [_trace_payload(trace) for trace in traces],
    }


def _trace_for_update(session: Session, action_id: str, recovery_kind: str) -> SearchJoinProtocolTrace | None:
    statement = select(SearchJoinProtocolTrace).where(
        SearchJoinProtocolTrace.action_id == action_id,
        SearchJoinProtocolTrace.recovery_kind == recovery_kind,
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    return session.scalar(statement)


def _trace_payload(trace: SearchJoinProtocolTrace) -> dict:
    return {
        "action_id": trace.action_id,
        "protocol_sample_version": trace.protocol_sample_version,
        "recovery_kind": trace.recovery_kind,
        "status": trace.status,
        "event_type": trace.event_type,
        "attempt_no": trace.attempt_no,
        "page_phase": trace.page_phase,
        "post_reset_page_phase": trace.post_reset_page_phase,
        "trace_summary": trace.trace_summary or {},
        "updated_at": trace.updated_at,
    }


def _update_reset_trace(trace: SearchJoinProtocolTrace, phase: str, result: dict) -> None:
    if trace.recovery_kind != HOT_LIST_RESET_KIND:
        return
    trace.post_reset_page_phase = phase
    trace.event_type = "post_reset_page_classified"
    trace.status = "reset_completed" if phase in {"search_category_page", "group_result_page"} else "reset_deviated"
    trace.trace_summary = {**(trace.trace_summary or {}), "post_reset": _safe_trace_summary(result)}


def _safe_trace_summary(result: dict) -> dict:
    trace = result.get("search_protocol_trace") if isinstance(result.get("search_protocol_trace"), dict) else {}
    return {
        "page_phase": str(result.get("jisou_page_phase") or trace.get("page_phase") or "unknown_page"),
        "layout": _safe_layout(trace.get("page") or trace.get("selector_page") or trace.get("result_page")),
    }


def _trace_event_type(result: dict, recovery_kind: str) -> str:
    if recovery_kind == HOT_LIST_RESET_KIND:
        return "post_reset_page_classified"
    return str(result.get("protocol_event_type") or "page_classified")


def _safe_layout(value: object) -> dict:
    source = value if isinstance(value, dict) else {}
    buttons = source.get("button_layout") if isinstance(source.get("button_layout"), list) else []
    return {"button_count": int(source.get("button_count") or 0), "button_layout": list(buttons)}


__all__ = [
    "HOT_LIST_RESET_KIND",
    "record_search_join_protocol_trace",
    "task_search_join_protocol_snapshot",
]
