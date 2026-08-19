from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select

from app.ai_gateway import AiProviderCredentials
from app.database import SessionLocal
from app.models import Action, AiAccountVoiceProfile, AiProvider, Task
from app.security import decrypt_secret
from app.services._common import ai_gateway
from app.services.task_center.message_brief import voice_contract_v3

TENANT_ID = 1
GENERATOR_PROVIDER_ID = 1
REVIEWER_PROVIDER_IDS = (2, 5)
DEFAULT_WINDOW_SIZE = 8
MAX_WINDOW_SIZE = 12
TASK_SCAN_LIMIT = 20
ACTION_SCAN_LIMIT = 800
REQUEST_TIMEOUT_SECONDS = 120
ROUTE_CONFIDENCE_MIN = 0.7
MAX_CANDIDATE_LENGTH = 24
CONTEXT_LINE_LIMIT = 12
MAX_CONTEXT_LENGTH = 600
ROUTE_BATCH_SIZE = 2
GENERATION_BATCH_SIZE = 4
REVIEW_BATCH_SIZE = 2
ADULT_ROUTES = frozenset({"adult_visual", "adult_product", "adult_service"})
ALL_ROUTES = frozenset({"general", *ADULT_ROUTES, "unsafe"})
CONTEXT_PII = re.compile(r"(?:https?://|t\.me/)\S+|@[A-Za-z0-9_]{3,}|\b\d{6,}\b|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
SPEAKER_PREFIX = re.compile(r"^[^:：\n]{1,40}[:：]\s*")
TASK_ROUTER_SYSTEM = """你只分类群运营配置，不写消息。仅当 topic_directions 或 teacher_targets 明确涉及成年视觉、性暗示、成人用品或成人服务时才允许成人方向；普通聊天、娱乐、课程和“老师”称呼都不能推断为成人。supported_routes 只能取 adult_visual、adult_product、adult_service。严格输出 JSON：{"tasks":[{"id":"原id","supported_routes":[],"confidence":0.0}]}。"""
CONTEXT_ROUTER_SYSTEM = """你只路由真实 Telegram 群上下文，不写回复。general=所有安全普通话题；没有成人信号绝不能判 unsafe。adult_visual=明确成年视觉或性暗示；adult_product=明确成人用品；adult_service=明确成人性服务者/成人“老师”语境。adult_service 中，最新重点为价格、空闲、区域、项目、时长、本人或预约时 reason_code=adult_service_inquiry；已确认成人性服务且最新重点为性感外观、身体湿润感或性暗示时 reason_code=adult_service_sensory。若同批多条成人服务上下文同时含交易信号和御姐、丝袜、性感或身体暗示，必须至少一条 inquiry、至少一条 sensory，不能让“上门/收费”覆盖感官信号。unsafe 仅限未成年人、高风险、空文本或乱码。“老师”单词本身、课程和夜课不能触发成人路由。其他 reason_code 只能取 general_topic、adult_visual_supported、adult_product_supported、minor_or_risk、empty_or_garbled。严格输出 JSON：{"routes":[{"case_id":"原id","route":"general|adult_visual|adult_product|adult_service|unsafe","confidence":0.0,"reason_code":"枚举"}]}。"""
REALIZER_SYSTEM = """你写一个真实 Telegram 活群窗口，每个 case 最多一条，必须接住最新上下文；voice_profile 只控制表达，不是事实。general：自然短反应或具体追问，禁止强转成人。adult_service_inquiry：像真实成年男性，只问价格、空闲、区域、项目、时长、照片本人、预约或怎么约中的一个。adult_service_sensory：上下文已确认是成人性服务“老师”并在聊性感、身体或性暗示；只评论人或身体感受，优先直接说“好润”或单点问“水多不？”，不要扩写，不得把润、湿修饰丝袜或衣物。adult_visual：短、直、感官或带欲望；允许“水滋滋、好润”，禁止“软软的、水灵灵、好心动、挺好看的”。adult_product：只接一个真实产品点。同一 candidates 内禁止相同或近义重复。禁止课程、夜课、教学、总结、广告腔、精致点评、统一句式、虚构经历或联系方式。没有自然且有依据的回复就 silence。严格输出 JSON：{"candidates":[{"case_id":"原id","action":"respond|silence","content":""}]}。"""
SENSORY_REALIZER_SYSTEM = """只为一个已确认的成年性服务语境写 Telegram 短回复。只写 2-6 个字，必须包含“润”或“水多”；不能出现丝袜、衣物、外貌、身材或主语。可直接写“好润”或问“水多不？”。严格输出 JSON：{"candidates":[{"case_id":"原id","action":"respond","content":""}]}。"""
REVIEW_SYSTEM = """你是严格的中文群聊 A/B 评审，不偏爱完整、礼貌或精致表达。结合 source、route、mode、voice_profile 比较 left/right：是否接住最近事实、像真人随手发、符合账号表达、成人方向只在允许语境出现、无虚构经历。adult_service_inquiry 应直接单点询问；adult_service_sensory 在明确成人性服务“老师”的性感/身体/性暗示语境中，像“好润”“水多不”这种粗粝短句应通过，不能因直白或不精致扣分；课程语境和 general 强转成人必须失败。分别给 natural/context/voice/route_fit 0-5 分；sendable 仅在可直接发送时为 true。winner 只能 left、right、tie。严格输出 JSON：{"results":[{"case_id":"原id","winner":"left|right|tie","left_sendable":true,"right_sendable":true,"left_scores":{"natural":0,"context":0,"voice":0,"route_fit":0},"right_scores":{"natural":0,"context":0,"voice":0,"route_fit":0},"codes":[]}]}。"""
AI_MARKERS = ("画面感", "氛围感", "让人", "仿佛", "值得", "这个内容", "这波操作", "感谢分享", "绝绝子")
CUTESY_MARKERS = ("软软的", "水灵灵", "好心动", "挺好看", "真不错")
COURSE_DRIFT = ("夜课", "课程", "教什么", "教学", "辅导员", "签到", "积分")
SERVICE_INTENTS = ("多少钱", "价格", "收费", "有空", "哪个区", "在哪里", "哪里", "包括什么", "都有什么", "一次多久", "多长时间", "有照片", "本人吗", "预约", "怎么约")
SENSORY_INTENTS = ("好润", "水多", "水滋滋", "湿不湿", "润不润", "够润", "真润")
SENSORY_CONTEXT_SIGNALS = ("御姐", "丝袜", "性感", "湿身", "身材", "胸", "臀", "骚", "润", "水多")
SENSORY_WRONG_OBJECTS = ("丝袜", "衣服", "裙子", "鞋")
EXPLICIT_ADULT_SERVICE_SIGNALS = ("楼凤",)
FORCED_ADULT_MARKERS = ("能约", "怎么约", "一次多久", "照片是本人", "摸一把", "馋了", "硬了", *SENSORY_INTENTS)
ROUTE_REASON_CODES = frozenset({"general_topic", "adult_visual_supported", "adult_product_supported", "adult_service_inquiry", "adult_service_sensory", "minor_or_risk", "empty_or_garbled"})
@dataclass(frozen=True)
class Direction:
    task: Task
    task_key: str
    allowed_routes: tuple[str, ...]
    confidence: float
@dataclass(frozen=True)
class Case:
    case_id: str
    context: str
    baseline: str
    account_id: int
    voice_profile: dict[str, Any]
    route: str = "unsafe"
    route_confidence: float = 0.0
    route_reason: str = "unrouted"
@dataclass(frozen=True)
class Candidate:
    case_id: str
    action: str
    content: str
def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:10]
def _structured(credentials: AiProviderCredentials, prompt: dict, system: str) -> tuple[dict, int]:
    payload, usage = ai_gateway.generate_structured(
        credentials,
        json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
        temperature=0.0,
        max_tokens=4096,
        system_prompt=system,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    if not isinstance(payload, dict):
        raise RuntimeError("structured_payload_invalid")
    return payload, int(usage.total_tokens or 0)

def _credentials(row: AiProvider, *, model_name: str = "") -> AiProviderCredentials:
    key = decrypt_secret(row.api_key_ciphertext)
    if not key:
        raise RuntimeError(f"provider_key_missing:{row.id}")
    return AiProviderCredentials(
        row.provider_name,
        row.provider_type,
        row.base_url,
        model_name or row.model_name,
        key,
        row.api_key_header,
    )
def _provider_bundle(session) -> tuple[AiProviderCredentials, tuple[AiProviderCredentials, ...]]:
    ids = (GENERATOR_PROVIDER_ID, *REVIEWER_PROVIDER_IDS)
    rows = {row.id: row for row in session.scalars(select(AiProvider).where(AiProvider.id.in_(ids)))}
    missing = [provider_id for provider_id in ids if provider_id not in rows]
    if missing:
        raise RuntimeError(f"shadow_provider_missing:{missing}")
    generator = _credentials(rows[GENERATOR_PROVIDER_ID], model_name="mimo-v2.5")
    reviewers = tuple(_credentials(rows[provider_id]) for provider_id in REVIEWER_PROVIDER_IDS)
    return generator, reviewers
def _running_tasks(session) -> list[Task]:
    return list(session.scalars(select(Task).where(
        Task.tenant_id == TENANT_ID,
        Task.type == "group_ai_chat",
        Task.status == "running",
        Task.deleted_at.is_(None),
    ).order_by(Task.updated_at.desc()).limit(TASK_SCAN_LIMIT)))
def _task_prompt_rows(tasks: list[Task]) -> list[dict[str, Any]]:
    rows = []
    for task in tasks:
        config = task.type_config if isinstance(task.type_config, dict) else {}
        rows.append({
            "id": _digest(task.id),
            "topic_directions": config.get("topic_directions") or [],
            "teacher_targets": config.get("teacher_targets") or [],
        })
    return rows

def _classify_tasks(tasks: list[Task], generator: AiProviderCredentials) -> tuple[list[Direction], int]:
    payload, tokens = _structured(generator, {"tasks": _task_prompt_rows(tasks)}, TASK_ROUTER_SYSTEM)
    raw_rows = payload.get("tasks")
    if not isinstance(raw_rows, list):
        raise RuntimeError("task_route_rows_missing")
    by_id = {str(row.get("id")): row for row in raw_rows if isinstance(row, dict)}
    directions = [_parse_direction(task, by_id.get(_digest(task.id))) for task in tasks]
    return directions, tokens
def _parse_direction(task: Task, row: object) -> Direction:
    if not isinstance(row, dict):
        raise RuntimeError(f"task_route_missing:{_digest(task.id)}")
    routes = tuple(dict.fromkeys(str(value) for value in (row.get("supported_routes") or [])))
    if any(route not in ADULT_ROUTES for route in routes):
        raise RuntimeError(f"task_route_invalid:{_digest(task.id)}")
    confidence = float(row.get("confidence") or 0.0)
    config_text = json.dumps(task.type_config or {}, ensure_ascii=False)
    if any(value in config_text for value in EXPLICIT_ADULT_SERVICE_SIGNALS):
        routes, confidence = tuple(dict.fromkeys((*routes, "adult_service"))), max(confidence, 1.0)
    return Direction(task, _digest(task.id), routes, confidence)

def _select_direction(directions: list[Direction], task_key: str) -> Direction:
    if task_key:
        matches = [item for item in directions if item.task_key.startswith(task_key)]
        if len(matches) != 1:
            raise RuntimeError(f"task_key_match_count:{len(matches)}")
        selected = matches[0]
    else:
        eligible = [item for item in directions if item.allowed_routes and item.confidence >= ROUTE_CONFIDENCE_MIN]
        if not eligible:
            raise RuntimeError("adult_direction_task_not_found")
        selected = eligible[0]
    if not selected.allowed_routes or selected.confidence < ROUTE_CONFIDENCE_MIN:
        raise RuntimeError(f"task_adult_direction_unproven:{selected.task_key}")
    return selected

def _safe_context(value: str) -> str:
    normalized = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    recent = [line for line in normalized if line][-CONTEXT_LINE_LIMIT:]
    redacted = [re.sub(r"\s+", " ", CONTEXT_PII.sub("", SPEAKER_PREFIX.sub("成员：", line))).strip() for line in recent]
    return "\n".join(line for line in redacted if line).strip()[:MAX_CONTEXT_LENGTH]
def _slim_voice(row: AiAccountVoiceProfile) -> dict[str, Any]:
    payload = voice_contract_v3(row)
    payload.pop("mask_name", None)
    payload.pop("summary", None)
    return payload
def _profile_map(session, account_ids: set[int]) -> dict[int, dict[str, Any]]:
    rows = session.scalars(select(AiAccountVoiceProfile).where(
        AiAccountVoiceProfile.tenant_id == TENANT_ID,
        AiAccountVoiceProfile.account_id.in_(account_ids),
        AiAccountVoiceProfile.status == "active",
        AiAccountVoiceProfile.quality_status == "active",
    ).order_by(AiAccountVoiceProfile.account_id.asc(), AiAccountVoiceProfile.version.desc()))
    profiles: dict[int, dict[str, Any]] = {}
    for row in rows:
        profiles.setdefault(row.account_id, _slim_voice(row))
    return profiles

def _action_rows(session, task_id: str) -> list[Action]:
    return list(session.scalars(select(Action).where(
        Action.tenant_id == TENANT_ID,
        Action.task_id == task_id,
        Action.action_type == "send_message",
        Action.status == "success",
        Action.account_id.is_not(None),
    ).order_by(Action.executed_at.desc(), Action.created_at.desc()).limit(ACTION_SCAN_LIMIT)))

def _load_cases(session, direction: Direction, window_size: int) -> list[Case]:
    actions = _action_rows(session, direction.task.id)
    account_ids = {int(action.account_id) for action in actions if action.account_id is not None}
    profiles = _profile_map(session, account_ids)
    cases, seen_accounts = [], set()
    for action in actions:
        case = _case_from_action(action, profiles)
        if case is None or case.account_id in seen_accounts:
            continue
        cases.append(case)
        seen_accounts.add(case.account_id)
        if len(cases) == window_size:
            return list(reversed(cases))
    raise RuntimeError(f"real_case_shortfall:{len(cases)}/{window_size}")

def _case_from_action(action: Action, profiles: dict[int, dict[str, Any]]) -> Case | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    account_id = int(action.account_id or 0)
    context = _safe_context(str(payload.get("ai_generation_history") or ""))
    baseline = re.sub(r"\s+", " ", str(payload.get("message_text") or "")).strip()[:100]
    if not account_id or not context or not baseline or account_id not in profiles:
        return None
    return Case(_digest(action.id), context, baseline, account_id, profiles[account_id])

def _route_cases(cases: list[Case], direction: Direction, generator: AiProviderCredentials) -> tuple[list[Case], int]:
    routed, tokens = [], 0
    for start in range(0, len(cases), ROUTE_BATCH_SIZE):
        batch = cases[start:start + ROUTE_BATCH_SIZE]
        rows = [{"case_id": case.case_id, "allowed_routes": list(direction.allowed_routes), "context": case.context} for case in batch]
        payload, used = _structured(generator, {"cases": rows}, CONTEXT_ROUTER_SYSTEM)
        raw_rows = payload.get("routes")
        if not isinstance(raw_rows, list):
            raise RuntimeError("context_route_rows_missing")
        by_id = {str(row.get("case_id")): row for row in raw_rows if isinstance(row, dict)}
        routed.extend(_apply_route(case, by_id.get(case.case_id), direction) for case in batch)
        tokens += used
    return _plan_service_modes(routed), tokens
def _plan_service_modes(cases: list[Case]) -> list[Case]:
    service = [case for case in cases if case.route == "adult_service"]
    if len(service) < 2 or any(case.route_reason == "adult_service_sensory" for case in service):
        return cases
    eligible = [case for case in service if any(value in case.context for value in SENSORY_CONTEXT_SIGNALS)]
    if not eligible:
        return cases
    chosen = max(eligible, key=lambda case: sum(case.context.rfind(value) + 1 for value in SENSORY_CONTEXT_SIGNALS))
    return [replace(case, route_reason="adult_service_sensory") if case.case_id == chosen.case_id else case for case in cases]

def _apply_route(case: Case, row: object, direction: Direction) -> Case:
    if not isinstance(row, dict):
        raise RuntimeError(f"context_route_missing:{case.case_id}")
    route = str(row.get("route") or "")
    confidence = float(row.get("confidence") or 0.0)
    reason = str(row.get("reason_code") or "missing_reason")[:40]
    if route not in ALL_ROUTES:
        raise RuntimeError(f"context_route_invalid:{case.case_id}")
    if route in ADULT_ROUTES and route not in direction.allowed_routes:
        raise RuntimeError(f"context_route_not_allowed:{case.case_id}:{route}")
    if reason not in ROUTE_REASON_CODES:
        raise RuntimeError(f"context_route_reason_invalid:{case.case_id}:{reason}")
    if route in ADULT_ROUTES and confidence < ROUTE_CONFIDENCE_MIN:
        return replace(case, route="unsafe", route_confidence=confidence, route_reason="adult_route_low_confidence")
    return replace(case, route=route, route_confidence=confidence, route_reason=reason)

def _generation_rows(cases: list[Case]) -> list[dict[str, Any]]:
    return [{
        "case_id": case.case_id,
        "route": case.route,
        "mode": case.route_reason,
        "source": case.context,
        "voice_profile": case.voice_profile,
    } for case in cases]

def _generate(cases: list[Case], generator: AiProviderCredentials) -> tuple[list[Candidate], int]:
    speaking = [case for case in cases if case.route != "unsafe"]
    candidates = [Candidate(case.case_id, "silence", "") for case in cases if case.route == "unsafe"]
    sensory = [case for case in speaking if case.route_reason == "adult_service_sensory"]
    regular = [case for case in speaking if case.route_reason != "adult_service_sensory"]
    batches = [[case] for case in sensory] + [regular[start:start + GENERATION_BATCH_SIZE] for start in range(0, len(regular), GENERATION_BATCH_SIZE)]
    tokens = 0
    for batch in batches:
        system = SENSORY_REALIZER_SYSTEM if batch[0].route_reason == "adult_service_sensory" else REALIZER_SYSTEM
        payload, used = _structured(generator, {"cases": _generation_rows(batch)}, system)
        rows = payload.get("candidates")
        if not isinstance(rows, list):
            raise RuntimeError("candidate_rows_missing")
        by_id = {str(row.get("case_id")): row for row in rows if isinstance(row, dict)}
        candidates.extend(_parse_candidate(case, by_id.get(case.case_id)) for case in batch)
        tokens += used
    by_id = {item.case_id: item for item in candidates}
    return [by_id[case.case_id] for case in cases], tokens


def _parse_candidate(case: Case, row: object) -> Candidate:
    if not isinstance(row, dict):
        raise RuntimeError(f"candidate_missing:{case.case_id}")
    action = str(row.get("action") or "")
    content = re.sub(r"\s+", " ", str(row.get("content") or "")).strip()
    if action not in {"respond", "silence"}:
        raise RuntimeError(f"candidate_action_invalid:{case.case_id}")
    if action == "respond" and not content:
        raise RuntimeError(f"candidate_content_missing:{case.case_id}")
    if action == "silence" and content:
        raise RuntimeError(f"silence_content_present:{case.case_id}")
    return Candidate(case.case_id, action, content)


def _normalized(value: str) -> str:
    return re.sub(r"[\s。！？!?~～，,]+", "", value).lower()
def _gate(case: Case, candidate: Candidate, accepted: list[str]) -> list[str]:
    content = candidate.content
    if candidate.action == "silence":
        return []
    failures = []
    if len(content) > MAX_CANDIDATE_LENGTH or "\n" in content:
        failures.append("length_or_line_break")
    if any(marker in content for marker in AI_MARKERS):
        failures.append("ai_tone")
    if _normalized(content) == _normalized(case.baseline):
        failures.append("same_as_baseline")
    if _normalized(content) in {_normalized(value) for value in accepted}:
        failures.append("duplicate")
    failures.extend(_route_gate(case, content))
    return list(dict.fromkeys(failures))
def _route_gate(case: Case, content: str) -> list[str]:
    if case.route == "adult_service":
        intents = SENSORY_INTENTS if case.route_reason == "adult_service_sensory" else SERVICE_INTENTS
        code = "sensory_intent_missing" if case.route_reason == "adult_service_sensory" else "service_intent_missing"
        failures = [code] if not any(value in content for value in intents) else []
        if case.route_reason == "adult_service_sensory" and any(value in content for value in SENSORY_WRONG_OBJECTS):
            failures.append("sensory_object_wrong")
        if any(value in content for value in COURSE_DRIFT):
            failures.append("course_drift")
        return failures
    if case.route == "adult_visual" and any(value in content for value in CUTESY_MARKERS):
        return ["cutesy_tone"]
    if case.route == "general" and any(value in content for value in FORCED_ADULT_MARKERS):
        return ["general_forced_adult"]
    return []
def _review_rows(cases: list[Case], candidates: list[Candidate], *, new_left: bool) -> list[dict[str, Any]]:
    candidate_map = {item.case_id: item for item in candidates}
    rows = []
    for case in cases:
        new = candidate_map[case.case_id].content
        left, right = (new, case.baseline) if new_left else (case.baseline, new)
        rows.append({
            "case_id": case.case_id,
            "source": case.context,
            "route": case.route,
            "mode": case.route_reason,
            "voice_profile": case.voice_profile,
            "left": left,
            "right": right,
        })
    return rows

def _review_once(credentials: AiProviderCredentials, cases: list[Case], candidates: list[Candidate], *, new_left: bool) -> tuple[dict[str, dict], int]:
    by_id, tokens = {}, 0
    for start in range(0, len(cases), REVIEW_BATCH_SIZE):
        batch = cases[start:start + REVIEW_BATCH_SIZE]
        rows = _review_rows(batch, candidates, new_left=new_left)
        payload, used = _structured(credentials, {"comparisons": rows}, REVIEW_SYSTEM)
        results = payload.get("results")
        if not isinstance(results, list):
            raise RuntimeError(f"review_rows_missing:{credentials.provider_name}")
        by_id.update({str(row.get("case_id")): row for row in results if isinstance(row, dict)})
        tokens += used
    missing = [case.case_id for case in cases if case.case_id not in by_id]
    if missing:
        raise RuntimeError(f"review_case_missing:{credentials.provider_name}:{missing}")
    return by_id, tokens


def _winner(row: dict, *, new_left: bool) -> str:
    value = str(row.get("winner") or "")
    if value not in {"left", "right", "tie"}:
        raise RuntimeError("review_winner_invalid")
    if value == "tie":
        return "tie"
    return "new" if (value == "left") == new_left else "baseline"


def _new_sendable(row: dict, *, new_left: bool) -> bool:
    return bool(row.get("left_sendable" if new_left else "right_sendable"))


def _reviewer_verdict(forward: dict, reverse: dict) -> dict[str, Any]:
    forward_winner = _winner(forward, new_left=True)
    reverse_winner = _winner(reverse, new_left=False)
    consistent = forward_winner == reverse_winner
    return {
        "position_consistent": consistent,
        "winner": forward_winner if consistent else "unproven",
        "new_sendable": _new_sendable(forward, new_left=True) and _new_sendable(reverse, new_left=False),
    }


def _review(cases: list[Case], candidates: list[Candidate], reviewers: tuple[AiProviderCredentials, ...]) -> tuple[dict[str, list[dict]], int]:
    verdicts: dict[str, list[dict]] = {case.case_id: [] for case in cases}
    tokens = 0
    for reviewer in reviewers:
        forward, used = _review_once(reviewer, cases, candidates, new_left=True)
        reverse, reversed_used = _review_once(reviewer, cases, candidates, new_left=False)
        tokens += used + reversed_used
        for case in cases:
            verdicts[case.case_id].append({
                "reviewer": reviewer.provider_name,
                **_reviewer_verdict(forward[case.case_id], reverse[case.case_id]),
            })
    return verdicts, tokens


def _consensus(rows: list[dict[str, Any]]) -> str:
    winners = {row["winner"] for row in rows}
    if not all(row["position_consistent"] for row in rows) or len(winners) != 1:
        return "unproven"
    return next(iter(winners))
def _case_reports(cases: list[Case], candidates: list[Candidate], verdicts: dict[str, list[dict]], show: bool) -> list[dict[str, Any]]:
    candidate_map = {item.case_id: item for item in candidates}
    reports, accepted = [], []
    for case in cases:
        candidate = candidate_map[case.case_id]
        gate_codes = _gate(case, candidate, accepted)
        if candidate.action == "respond" and not gate_codes:
            accepted.append(candidate.content)
        reviews = verdicts.get(case.case_id, []) if candidate.action == "respond" else []
        report = {
            "case_id": case.case_id,
            "route": case.route,
            "route_confidence": case.route_confidence,
            "route_reason": case.route_reason,
            "action": candidate.action,
            "gate_codes": gate_codes,
            "review_consensus": _consensus(reviews) if reviews else ("silence" if candidate.action == "silence" else "unproven"),
            "review_sendable": bool(reviews) and all(row["new_sendable"] for row in reviews),
            "reviewers": reviews,
        }
        if show:
            report["candidate"] = candidate.content
        reports.append(report)
    return reports
def _metrics(reports: list[dict[str, Any]], cases: list[Case], candidates: list[Candidate]) -> dict[str, Any]:
    responses = [row for row in reports if row["action"] == "respond"]
    response_ids = {row["case_id"] for row in responses}
    contents = [item.content for item in candidates if item.case_id in response_ids]
    route_counts = Counter(row["route"] for row in reports)
    consensus = Counter(row["review_consensus"] for row in responses)
    adult_count = sum(route_counts[route] for route in ADULT_ROUTES)
    return {
        "case_count": len(reports),
        "response_count": len(responses),
        "silence_count": len(reports) - len(responses),
        "route_counts": dict(sorted(route_counts.items())),
        "adult_route_share": round(adult_count / len(reports), 3) if reports else 0.0,
        "gate_failure_count": sum(bool(row["gate_codes"]) for row in responses),
        "general_forced_adult_count": sum("general_forced_adult" in row["gate_codes"] for row in responses),
        "duplicate_count": sum("duplicate" in row["gate_codes"] for row in responses),
        "review_sendable_count": sum(row["review_sendable"] for row in responses),
        "comparison": dict(sorted(consensus.items())),
        "voice_profile_variants": len({json.dumps(case.voice_profile, sort_keys=True) for case in cases}),
        "candidate_unique_rate": round(len({_normalized(value) for value in contents}) / len(contents), 3) if contents else None,
        "mean_candidate_length": round(sum(map(len, contents)) / len(contents), 2) if contents else None,
    }
def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only production-shaped AI group mixed-content shadow")
    parser.add_argument("--task-key", default="", help="Optional task hash prefix; defaults to latest explicit adult-direction task")
    parser.add_argument("--window-size", type=int, default=DEFAULT_WINDOW_SIZE)
    parser.add_argument("--show-candidates", action="store_true")
    parser.add_argument("--generation-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.window_size <= MAX_WINDOW_SIZE:
        parser.error(f"--window-size must be between 1 and {MAX_WINDOW_SIZE}")
    return args
def main() -> None:
    args = _args()
    started = time.monotonic()
    with SessionLocal() as session:
        generator, reviewers = _provider_bundle(session)
        directions, task_tokens = _classify_tasks(_running_tasks(session), generator)
        direction = _select_direction(directions, args.task_key)
        cases = _load_cases(session, direction, args.window_size)
    cases, route_tokens = _route_cases(cases, direction, generator)
    candidates, generation_tokens = _generate(cases, generator)
    reviewable_cases = [case for case in cases if next(item for item in candidates if item.case_id == case.case_id).action == "respond"]
    reviewable_candidates = [item for item in candidates if item.action == "respond"]
    verdicts, review_tokens = _review(reviewable_cases, reviewable_candidates, reviewers) if reviewable_cases and not args.generation_only else ({}, 0)
    reports = _case_reports(cases, candidates, verdicts, args.show_candidates)
    print(json.dumps({
        "shadow": "ai_group_mixed_v1",
        "task_key": direction.task_key,
        "allowed_adult_routes": list(direction.allowed_routes),
        "direction_confidence": direction.confidence,
        "generator": generator.provider_name,
        "reviewers": [item.provider_name for item in reviewers],
        "review_status": "skipped_by_flag" if args.generation_only else "executed",
        "metrics": _metrics(reports, cases, candidates),
        "cases": reports,
        "tokens": task_tokens + route_tokens + generation_tokens + review_tokens,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "writes_database": False,
        "sends_telegram": False,
        "prints_source_context": False,
        "prints_production_baseline": False,
    }, ensure_ascii=False, indent=2, sort_keys=True))
if __name__ == "__main__":
    main()
