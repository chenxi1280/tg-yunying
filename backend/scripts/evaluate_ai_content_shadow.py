from __future__ import annotations
import argparse
import hashlib
import json
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any
from sqlalchemy import select
from app.ai_gateway import AiProviderCredentials, normalize_ai_model_name
from app.database import SessionLocal
from app.models import Action, AiProvider, Task, TenantAiSetting
from app.security import decrypt_secret
from app.services._common import ai_gateway
from app.services.ai_config import ai_provider_credentials
from app.services.task_center.ai_generator import (
    CHANNEL_COMMENT_PURPOSE,
    _channel_comment_system_prompt,
    _prompt_profile,
    _sanitize_sensitive_context,
)
from app.services.task_center.ai_group_prompt import build_group_prompt, sanitize_group_messages
from app.services.task_center.ai_quality_evaluation import aggregate_position_swap
DEFAULT_MODEL = "MiniMax-M3"
CANDIDATE_MODEL = "MiniMax-M2.5"
DEFAULT_SAMPLE_COUNT = 3
PROVIDER_TIMEOUT_SECONDS = 90
MAX_QUERY_ROWS = 200
JUDGE_BATCH_SIZE = 6
GENERIC_MARKERS = ("确实", "感觉", "不错", "看起来", "挺好", "值得", "期待", "可以关注")
TERMINAL_PUNCTUATION = "。！？!?"
@dataclass(frozen=True)
class Options:
    tenant_id: int
    group_samples: int
    comment_samples: int
    execute_provider_calls: bool
    generator_model: str
    candidate_model: str
    candidate_provider_id: int
    judge_model: str
    skip_judge: bool
    show_content: bool

@dataclass(frozen=True)
class EvalCase:
    case_id: str
    kind: str
    context_cluster: str
    context: str
    baseline: str
    task_config: dict[str, Any]

@dataclass(frozen=True)
class Variant:
    name: str
    prompt_style: str
    model_name: str

@dataclass(frozen=True)
class Generated:
    case_id: str
    kind: str
    variant: str
    content: str
    prompt_chars: int
    total_tokens: int
    duration_ms: int
    error_code: str

@dataclass(frozen=True)
class PairItem:
    pair_id: str
    context_cluster: str
    context: str
    candidate: str
    baseline: str
    comparison: str

def parse_args() -> Options:
    parser = argparse.ArgumentParser(description="Read-only production-shaped AI content shadow evaluation")
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--group-samples", type=int, default=DEFAULT_SAMPLE_COUNT)
    parser.add_argument("--comment-samples", type=int, default=DEFAULT_SAMPLE_COUNT)
    parser.add_argument("--execute-provider-calls", action="store_true")
    parser.add_argument("--generator-model", default=DEFAULT_MODEL)
    parser.add_argument("--candidate-model", default=CANDIDATE_MODEL)
    parser.add_argument("--candidate-provider-id", type=int, default=0)
    parser.add_argument("--judge-model", default=DEFAULT_MODEL)
    parser.add_argument("--skip-judge", action="store_true")
    parser.add_argument("--show-content", action="store_true")
    args = parser.parse_args()
    return Options(
        tenant_id=args.tenant_id,
        group_samples=max(0, args.group_samples),
        comment_samples=max(0, args.comment_samples),
        execute_provider_calls=bool(args.execute_provider_calls),
        generator_model=normalize_ai_model_name(args.generator_model),
        candidate_model=normalize_ai_model_name(args.candidate_model),
        candidate_provider_id=max(0, args.candidate_provider_id),
        judge_model=normalize_ai_model_name(args.judge_model),
        skip_judge=bool(args.skip_judge),
        show_content=bool(args.show_content),
    )
def _recent_action_rows(session, options: Options, task_type: str) -> list[tuple[Action, Task]]:
    action_type = "post_comment" if task_type == "channel_comment" else "send_message"
    tasks = list(session.scalars(
        select(Task)
        .where(
            Task.tenant_id == options.tenant_id,
            Task.type == task_type,
            Task.deleted_at.is_(None),
        )
        .order_by(Task.updated_at.desc())
        .limit(20)
    ))
    if not tasks:
        return []
    task_map = {row.id: row for row in tasks}
    actions = list(session.scalars(
        select(Action)
        .where(
            Action.tenant_id == options.tenant_id,
            Action.task_id.in_(task_map),
            Action.action_type == action_type,
            Action.status == "success",
        )
        .order_by(Action.created_at.desc())
        .limit(MAX_QUERY_ROWS)
    ))
    return [(action, task_map[action.task_id]) for action in actions]
def _case_id(action_id: str, kind: str) -> str:
    return hashlib.sha256(f"{kind}:{action_id}".encode()).hexdigest()[:12]
def _group_case(action: Action, task: Task) -> EvalCase | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    context = str(payload.get("ai_generation_history") or "").strip()
    baseline = str(payload.get("message_text") or "").strip()
    if not context or not baseline:
        return None
    return EvalCase(
        case_id=_case_id(action.id, "group"),
        kind="group",
        context_cluster=str(payload.get("group_id") or task.id),
        context=context,
        baseline=baseline,
        task_config=dict(task.type_config or {}),
    )
def _comment_case(action: Action, task: Task) -> EvalCase | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    context = str(payload.get("message_content") or "").strip()
    baseline = str(payload.get("comment_text") or "").strip()
    if not context or not baseline:
        return None
    cluster = str(payload.get("channel_message_id") or payload.get("message_id") or task.id)
    return EvalCase(
        case_id=_case_id(action.id, "comment"),
        kind="comment",
        context_cluster=cluster,
        context=context,
        baseline=baseline,
        task_config=dict(task.type_config or {}),
    )
def _select_cases(rows: list[tuple[Action, Task]], kind: str, limit: int) -> list[EvalCase]:
    if limit <= 0:
        return []
    builder = _group_case if kind == "group" else _comment_case
    selected: list[EvalCase] = []
    seen_clusters: set[str] = set()
    for action, task in rows:
        case = builder(action, task)
        if case is None or case.context_cluster in seen_clusters:
            continue
        selected.append(case)
        seen_clusters.add(case.context_cluster)
        if len(selected) >= limit:
            break
    return selected

def load_inputs(options: Options) -> tuple[list[EvalCase], tuple[AiProviderCredentials, AiProviderCredentials]]:
    with SessionLocal() as session:
        setting = session.scalar(select(TenantAiSetting).where(TenantAiSetting.tenant_id == options.tenant_id))
        provider = session.get(AiProvider, setting.default_provider_id) if setting else None
        if not setting or not setting.ai_enabled or provider is None:
            raise RuntimeError("active_ai_provider_missing")
        candidate_provider = session.get(AiProvider, options.candidate_provider_id) if options.candidate_provider_id else provider
        if candidate_provider is None:
            raise RuntimeError("candidate_ai_provider_missing")
        candidate_key = decrypt_secret(candidate_provider.api_key_ciphertext)
        if not candidate_key:
            raise RuntimeError("candidate_ai_provider_key_missing")
        group_rows = _recent_action_rows(session, options, "group_ai_chat")
        comment_rows = _recent_action_rows(session, options, "channel_comment")
        cases = [
            *_select_cases(group_rows, "group", options.group_samples),
            *_select_cases(comment_rows, "comment", options.comment_samples),
        ]
        candidate_credentials = AiProviderCredentials(
            candidate_provider.provider_name, candidate_provider.provider_type,
            candidate_provider.base_url, candidate_provider.model_name,
            candidate_key, candidate_provider.api_key_header,
        )
        credentials = (ai_provider_credentials(provider), candidate_credentials)
    return cases, credentials

def variants(options: Options) -> tuple[Variant, ...]:
    return (
        Variant("current_m3", "current", options.generator_model),
        Variant("compact_m3", "compact", options.generator_model),
        Variant("compact_candidate", "compact", options.candidate_model),
    )

def _compact_group_prompt(case: EvalCase) -> tuple[str, str]:
    messages = sanitize_group_messages(case.context.splitlines())[-5:]
    voice = str(case.task_config.get("account_voice_profile_summary") or "").strip()
    system = (
        "你是Telegram普通群友，只回一句。承接最近一条具体内容；4到20个中文字符；"
        "允许口语、省略主语和省略句末标点。不要总结、解释、客套、运营腔、编经历或重复别人整句。"
        "不得涉及交易、联系方式、位置、服务、未成年人或露骨内容。只输出JSON。"
    )
    payload = {"recent_messages": messages, "voice_hint": voice[:120]}
    user = f"{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n输出：{{\"drafts\":[{{\"persona\":\"群友\",\"content\":\"一句话\",\"risk_level\":\"低\"}}]}}"
    return system, user

def _current_group_prompt(case: EvalCase) -> tuple[str, str]:
    bundle = build_group_prompt(
        case.task_config,
        target_label="生产群",
        history=case.context,
        count=1,
    )
    return bundle.system_prompt, bundle.user_prompt

def _comment_requirements(case: EvalCase) -> str:
    config = case.task_config
    return (
        f"频道消息：{_sanitize_sensitive_context(case.context)}\n"
        f"评论风格：{config.get('comment_style') or 'mixed'}\n"
        f"语言：{config.get('language') or 'zh-CN'}"
    )

def _current_comment_prompt(case: EvalCase) -> tuple[str, str]:
    topic = case.task_config.get("topic_hint") or "频道评论"
    prompt, _, _ = _prompt_profile(
        count=1,
        purpose=CHANNEL_COMMENT_PURPOSE,
        target_label="生产频道",
        topic=topic,
        requirements=_comment_requirements(case),
    )
    return _channel_comment_system_prompt(), prompt

def _compact_comment_prompt(case: EvalCase) -> tuple[str, str]:
    source = _sanitize_sensitive_context(case.context)
    system = (
        "你是Telegram频道评论区的真实读者，只写一句短评。必须回应原文一个具体细节；"
        "4到22个中文字符；可以追问、惊讶、赞同或补一句，但不要泛泛夸奖、总结、编经历或写完整小作文。"
        "允许口语和省略句末标点。只输出JSON。"
    )
    user = f"原文：{source}\n输出：{{\"drafts\":[{{\"persona\":\"读者\",\"content\":\"一句话\",\"risk_level\":\"低\"}}]}}"
    return system, user

def prompts(case: EvalCase, variant: Variant) -> tuple[str, str]:
    if case.kind == "group":
        return _current_group_prompt(case) if variant.prompt_style == "current" else _compact_group_prompt(case)
    return _current_comment_prompt(case) if variant.prompt_style == "current" else _compact_comment_prompt(case)

def _generation_topic(case: EvalCase) -> str:
    if case.kind == "group":
        return " ".join(sanitize_group_messages(case.context.splitlines())[-5:])
    return _sanitize_sensitive_context(case.context)[:500]

def generate_one(case: EvalCase, variant: Variant, base_credentials: AiProviderCredentials) -> Generated:
    system_prompt, user_prompt = prompts(case, variant)
    credentials = replace(base_credentials, model_name=variant.model_name)
    started = time.monotonic()
    try:
        result = ai_gateway.generate_drafts(
            credentials,
            user_prompt,
            count=1,
            topic=_generation_topic(case),
            tone="casual",
            persona_set=["普通用户"],
            temperature=0.8,
            max_tokens=1024,
            system_prompt=system_prompt,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
        content = str(result.candidates[0].content or "").strip() if result.candidates else ""
        tokens = int(result.usage.total_tokens or 0)
        error_code = "" if content else "empty_candidate"
        if content.lower().startswith("the request was rejected"):
            content, error_code = "", "provider_safety_rejection"
    except Exception as exc:  # noqa: BLE001
        content, tokens, error_code = "", 0, _safe_error_code(exc)
    return Generated(
        case.case_id,
        case.kind,
        variant.name,
        content,
        len(system_prompt) + len(user_prompt),
        tokens,
        round((time.monotonic() - started) * 1000),
        error_code,
    )

def _safe_error_code(exc: Exception) -> str:
    detail = str(exc).lower()
    if "malformed" in detail or "json" in detail or "candidate" in detail:
        return "malformed_output"
    if "timed out" in detail or "timeout" in detail or "deadline" in detail:
        return "provider_timeout"
    if "http" in detail:
        return f"provider_http_{match.group(1)}" if (match := re.search(r"http\s+(\d{3})", detail)) else "provider_http_error"
    if "no choices" in detail or "empty" in detail:
        return "empty_output"
    return type(exc).__name__

def generate_all(cases: list[EvalCase], options: Options, credentials: tuple[AiProviderCredentials, AiProviderCredentials]) -> list[Generated]:
    primary, candidate = credentials
    return [
        generate_one(case, variant, candidate if variant.name == "compact_candidate" else primary)
        for case in cases
        for variant in variants(options)
    ]

def _outputs_by_case(rows: list[Generated]) -> dict[tuple[str, str], Generated]:
    return {(row.case_id, row.variant): row for row in rows if row.content}

def build_pairs(cases: list[EvalCase], rows: list[Generated]) -> list[PairItem]:
    outputs = _outputs_by_case(rows)
    comparisons = (
        ("compact_vs_current_m3", "compact_m3", "current_m3"),
        ("candidate_vs_m3_compact", "compact_candidate", "compact_m3"),
    )
    pairs: list[PairItem] = []
    for case in cases:
        for comparison, candidate_name, baseline_name in comparisons:
            candidate = outputs.get((case.case_id, candidate_name))
            baseline = outputs.get((case.case_id, baseline_name))
            if not candidate or not baseline:
                continue
            scoped_comparison = f"{case.kind}:{comparison}"
            pair_id = f"{case.case_id}:{scoped_comparison}"
            pairs.append(PairItem(pair_id, case.context_cluster, case.context, candidate.content, baseline.content, scoped_comparison))
    return pairs

def _judge_payload(pairs: list[PairItem], reverse: bool) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for pair in pairs:
        first, second = (pair.baseline, pair.candidate) if reverse else (pair.candidate, pair.baseline)
        payload.append({"id": pair.pair_id, "context": pair.context[:500], "A": first, "B": second})
    return payload

def _judge_batch(pairs: list[PairItem], credentials: AiProviderCredentials, model_name: str, reverse: bool) -> dict[str, dict]:
    system = (
        "你是严格的中文社交内容盲评员。逐项比较A/B，忽略位置和长度；"
        "重点看是否承接上下文、像真人随手说话、是否口语自然且不模板化。"
        "惩罚泛泛夸奖、完整小作文、AI总结腔、重复原文和编造事实。平局可以接受。只输出JSON。"
    )
    prompt = (
        f"样本：{json.dumps(_judge_payload(pairs, reverse), ensure_ascii=False, separators=(',', ':'))}\n"
        "输出：{\"results\":[{\"id\":\"样本id\",\"winner\":\"A|B|TIE\",\"confidence\":0.0,\"evidence\":[\"短原因码\"]}]}"
    )
    payload, _ = ai_gateway.generate_structured(
        replace(credentials, model_name=model_name),
        prompt,
        temperature=0.1,
        max_tokens=4096,
        system_prompt=system,
        timeout=PROVIDER_TIMEOUT_SECONDS,
    )
    results = payload.get("results") if isinstance(payload, dict) else payload if isinstance(payload, list) else None
    if not isinstance(results, list):
        raise RuntimeError("judge_results_missing")
    return {str(item.get("id")): item for item in results if isinstance(item, dict)}

def judge(pairs: list[PairItem], credentials: AiProviderCredentials, model_name: str, reverse: bool) -> dict[str, dict]:
    results: dict[str, dict] = {}
    for start in range(0, len(pairs), JUDGE_BATCH_SIZE):
        batch = pairs[start:start + JUDGE_BATCH_SIZE]
        results.update(_judge_batch(batch, credentials, model_name, reverse))
    return results

def pairwise_summary(pairs: list[PairItem], forward: dict[str, dict], reverse: dict[str, dict]) -> dict[str, Any]:
    grouped: dict[str, Counter] = {}
    consistency: dict[str, list[bool]] = {}
    for pair in pairs:
        if pair.pair_id not in forward or pair.pair_id not in reverse:
            continue
        result = aggregate_position_swap(forward[pair.pair_id], reverse[pair.pair_id])
        grouped.setdefault(pair.comparison, Counter())[result.winner] += 1
        consistency.setdefault(pair.comparison, []).append(result.position_consistent)
    return {
        name: {
            "outcomes": dict(counts),
            "position_consistency_rate": round(sum(consistency[name]) / len(consistency[name]), 3),
        }
        for name, counts in grouped.items()
    }

def _bigrams(value: str) -> set[str]:
    compact = re.sub(r"\s+", "", value)
    return {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}

def _context_overlap(content: str, context: str) -> float:
    content_tokens = _bigrams(content)
    if not content_tokens:
        return 0.0
    return len(content_tokens & _bigrams(context)) / len(content_tokens)

def _structure(value: str) -> str:
    compact = re.sub(r"\s+", "", value)
    length_bucket = min(8, len(compact) // 4)
    punctuation = "question" if "?" in compact or "？" in compact else "statement"
    ending = compact[-1:] if compact[-1:] in TERMINAL_PUNCTUATION else "open"
    return f"{compact[:2]}:{length_bucket}:{punctuation}:{ending}"

def _variant_metrics(items: list[Generated], errors: Counter, contexts: dict[str, str]) -> dict[str, Any]:
    lengths = [len(row.content) for row in items]
    overlaps = [_context_overlap(row.content, contexts[row.case_id]) for row in items]
    return {
        "success_count": len(items),
        "error_counts": dict(errors),
        "mean_length": round(statistics.mean(lengths), 2) if lengths else None,
        "terminal_punctuation_rate": round(sum(row.content[-1:] in TERMINAL_PUNCTUATION for row in items) / len(items), 3) if items else None,
        "question_rate": round(sum("?" in row.content or "？" in row.content for row in items) / len(items), 3) if items else None,
        "generic_marker_rate": round(sum(any(marker in row.content for marker in GENERIC_MARKERS) for row in items) / len(items), 3) if items else None,
        "structure_unique_rate": round(len({_structure(row.content) for row in items}) / len(items), 3) if items else None,
        "mean_context_bigram_overlap": round(statistics.mean(overlaps), 3) if overlaps else None,
        "mean_prompt_chars": round(statistics.mean(row.prompt_chars for row in items), 1) if items else None,
        "total_tokens": sum(row.total_tokens for row in items),
        "mean_duration_ms": round(statistics.mean(row.duration_ms for row in items), 1) if items else None,
    }

def metric_summary(cases: list[EvalCase], rows: list[Generated]) -> dict[str, Any]:
    contexts = {case.case_id: case.context for case in cases}
    result: dict[str, Any] = {}
    keys = sorted({(row.kind, row.variant) for row in rows})
    for kind, variant in keys:
        scoped = [row for row in rows if row.kind == kind and row.variant == variant]
        items = [row for row in scoped if row.content]
        errors = Counter(row.error_code for row in scoped if row.error_code)
        result[f"{kind}:{variant}"] = _variant_metrics(items, errors, contexts)
    return result

def dry_run_report(cases: list[EvalCase], options: Options) -> dict[str, Any]:
    counts = Counter(case.kind for case in cases)
    shapes = Counter((variant.prompt_style, variant.model_name) for variant in variants(options))
    return {
        "mode": "dry_run",
        "case_counts": dict(counts),
        "variants": [{"prompt_style": style, "model": model, "count": count} for (style, model), count in shapes.items()],
        "writes_database": False,
        "sends_telegram": False,
        "prints_content": False,
    }

def execute_report(cases: list[EvalCase], options: Options, credentials: tuple[AiProviderCredentials, AiProviderCredentials]) -> dict[str, Any]:
    generated = generate_all(cases, options, credentials)
    pairs = build_pairs(cases, generated)
    judge_credentials = credentials[0]
    judge_error = ""
    try:
        forward = judge(pairs, judge_credentials, options.judge_model, reverse=False) if pairs and not options.skip_judge else {}
        reverse = judge(pairs, judge_credentials, options.judge_model, reverse=True) if pairs and not options.skip_judge else {}
    except Exception as exc:  # noqa: BLE001
        forward, reverse, judge_error = {}, {}, _safe_error_code(exc)
    return {
        "mode": "provider_shadow",
        "case_counts": dict(Counter(case.kind for case in cases)),
        "metrics": metric_summary(cases, generated),
        "pairwise": pairwise_summary(pairs, forward, reverse),
        "judge_error_code": judge_error,
        "judge_model": options.judge_model,
        "judge_independent_from_all_generators": options.judge_model not in {options.generator_model, options.candidate_model},
        "content": [
            {"case_id": row.case_id, "kind": row.kind, "variant": row.variant,
             "content": _sanitize_sensitive_context(row.content)[:80]}
            for row in generated if options.show_content and row.content
        ],
        "writes_database": False,
        "sends_telegram": False,
        "prints_content": options.show_content,
    }

def main() -> None:
    options = parse_args()
    cases, credentials = load_inputs(options)
    report = (
        execute_report(cases, options, credentials)
        if options.execute_provider_calls
        else dry_run_report(cases, options)
    )
    print(json.dumps({"AI_CONTENT_SHADOW_EVAL": report}, ensure_ascii=False, sort_keys=True))

if __name__ == "__main__":
    main()
