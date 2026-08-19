from __future__ import annotations

import hashlib
import json
import os
import re
import statistics
import time
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select

from app.ai_gateway import AiProviderCredentials
from app.database import SessionLocal
from app.models import Action, AiAccountVoiceProfile, AiProvider, ChannelMessage, Task
from app.security import decrypt_secret
from app.services._common import ai_gateway
from app.services.task_center.ai_group_prompt import safe_clauses, sanitize_group_messages
from app.services.task_center.message_brief import voice_contract_v3

TENANT_ID = 1
MIMO_PROVIDER_ID = 1
REVIEWER_PROVIDER_IDS = (2, 4, 5)
MAX_ACTION_ROWS = 200
REQUEST_TIMEOUT_SECONDS = 120
MAX_SLOT_ATTEMPTS = 2
MICRO_BATCH_SIZE = 4
SEMANTIC_REVIEW_ROUNDS = 3
SEMANTIC_REVIEW_CONFIDENCE = 0.8
POLISHED_MARKERS = (
    "画面感", "太形象", "太生动", "整体", "氛围", "质感", "让人", "仿佛", "值得",
    "确实", "看起来", "感觉", "这个内容", "这个细节", "这波操作", "感谢分享",
    "懂的都懂", "破防了", "绝绝子", "好绝", "绝了", "狠狠爱", "笑死",
)
UNSUPPORTED_MARKERS = (
    "明天", "昨天", "上次", "下次", "以前", "我试过", "我用过", "去过",
    "预定", "报名了", "下单", "已经买",
)
@dataclass(frozen=True)
class Scene:
    kind: str
    case_id: str
    context: str
    voice_profiles: tuple[dict[str, Any], ...] = ()


BRIEFS = (
    {"slot": "a", "speech_act": "pure_emotion", "length": "1-3", "punctuation": "none", "rule": "不复述source名词"},
    {"slot": "b", "speech_act": "specific_echo", "length": "3-5", "punctuation": "none", "rule": "只抓一个source关键词"},
    {"slot": "c", "speech_act": "small_question", "length": "8-14", "punctuation": "question", "rule": "问一个具体小点"},
    {"slot": "d", "speech_act": "restrained_agreement", "length": "2-5", "punctuation": "none", "rule": "不复述source名词"},
    {"slot": "e", "speech_act": "light_tease", "length": "7-12", "punctuation": "none", "rule": "调侃状态但不重复原句"},
    {"slot": "f", "speech_act": "follow_up", "length": "9-16", "punctuation": "none", "rule": "不编时间经历或结果"},
    {"slot": "g", "speech_act": "curious_reaction", "length": "7-12", "punctuation": "question", "rule": "与c问法不同"},
    {"slot": "h", "speech_act": "pure_emotion", "length": "2-4", "punctuation": "none", "rule": "不复述source名词"},
)

ADULT_VISUAL_BRIEFS = (
    {"slot": "a", "speech_act": "raw_micro_praise", "length": "2-4", "punctuation": "none", "rule": "直给短夸，不用甜美形容词"},
    {"slot": "b", "speech_act": "wet_sensory_fragment", "length": "3-7", "punctuation": "none", "rule": "只说水润感，用男性口语"},
    {"slot": "c", "speech_act": "tender_sensory_fragment", "length": "3-7", "punctuation": "none", "rule": "只表达嫩、滑或馋，不用水、滋、润，不卖萌"},
    {"slot": "d", "speech_act": "blunt_desire", "length": "4-8", "punctuation": "none", "rule": "表达想触碰的欲望，不用美、水、滋、润、嫩，不写小作文"},
    {"slot": "e", "speech_act": "physical_reaction", "length": "4-9", "punctuation": "none", "rule": "成年男性身体或欲望反应，不用锚点词和谁能忍、谁顶得住"},
    {"slot": "f", "speech_act": "lust_level_reaction", "length": "4-9", "punctuation": "none", "rule": "只表达欲望变强或越来越想要，禁止引用source词"},
    {"slot": "g", "speech_act": "raw_specific_reaction", "length": "5-10", "punctuation": "none", "rule": "只接source一个真实可见细节，不编角度姿势，不问约见"},
    {"slot": "h", "speech_act": "raw_micro_reaction", "length": "2-5", "punctuation": "none", "rule": "粗粝极短反应，避开前槽措辞"},
)
ADULT_PRODUCT_BRIEFS = (
    {"slot": "a", "speech_act": "product_feature_question", "length": "5-10", "punctuation": "question", "rule": "只问原文明确写出的一个功能"},
    {"slot": "b", "speech_act": "product_feel_question", "length": "4-9", "punctuation": "question", "rule": "只问软硬、力度或触感中的一个相关项"},
    {"slot": "c", "speech_act": "product_spec_question", "length": "5-11", "punctuation": "question", "rule": "只问尺寸、材质、档位中的一个相关项"},
    {"slot": "d", "speech_act": "product_practical_question", "length": "5-11", "punctuation": "question", "rule": "只问噪音、续航、防水、清洁中的一个相关项"},
    {"slot": "e", "speech_act": "product_intensity_reaction", "length": "5-9", "punctuation": "none", "rule": "像“十档也太狠了”，属性加口语反应，不照抄参数"},
    {"slot": "f", "speech_act": "product_practical_reaction", "length": "5-10", "punctuation": "none", "rule": "像“静音这个实用”，只认可原文明写的实用属性"},
    {"slot": "g", "speech_act": "purchase_question", "length": "5-11", "punctuation": "question", "rule": "只问价格、发货速度或隐私包装，不问配件材质"},
    {"slot": "h", "speech_act": "product_micro_question", "length": "3-8", "punctuation": "question", "rule": "只问一个尚未出现的产品小点"},
)
ADULT_VISUAL_SYSTEM = """
当前 scene 是明确面向成年人的私密视觉内容评论区。“老师”只是成人称呼，不代表学校、课程或教学。
目标是截图里成年男性随手评论的粗粝口吻：短、直、感官化、带欲望，允许省略句、性暗示和不精致表达。
人工批准锚点只保留“真美”“水滋滋”“好润”；只学密度和语感，整组最多原样使用两个，其他槽不得复写锚点。
禁止甜宠卖萌、温柔审美、小红书文案和完整点评；不得出现“软软的”“水灵灵的”“心动”“挺好看的”“真不错”。
禁止转成夜课、课程、教学、辅导员、积分、签到、排名或活动讨论；禁止约见、未成年人、学生身份和联系方式。"""
ADULT_PRODUCT_SYSTEM = """
当前 scene 是明确面向成年人的实体用品内容。只围绕 source 里的产品及其可能相关的功能、尺寸、材质、力度、噪音、续航、防水、清洁、价格或包装说话。
像真实男性买家随手问一句：短、直接、带一点成人口吻，但核心必须是产品；每条只能说或问一个点。语感参考：硅胶软不软、十档差别大吗、声音大不大、包装看不出来吧。
禁止“看着不错”“挺好”“听着带劲”这类空泛评价；短反应可写“十档也太狠了”“静音这个实用”，不得只抄产品参数。
禁止编“我用过/买过”，禁止评论人物外观、约见、课程教学、联系方式和未成年人；禁止把用品帖子写成纯色情画面评论。"""
VOICE_PROFILE_SYSTEM = """
每个 brief 的 voice_profile 来自真实线上账号面具。必须让不同槽在句长、语气、问句率、句尾语气词和表情频率上可区分。
面具只控制表达方式，不是可陈述事实；不得复述面具名称、身份框架、经历、消费信息或偏好标签。"""
SEMANTIC_REVIEW_SYSTEM = """你是严格的中文真人评论质量审查员，逐槽判断候选是否可直接发在成年人视觉内容评论区。
pass 必须像成年男性随手敲的粗粝评论：短、直、感官化或带欲望，允许省略、粗口语和性暗示，不得因为“不精致”而失败。
“真美”“水滋滋”“好润”“好嫩好润”“想摸下”“欲望上来了”可通过；“嘴唇软软的”“水灵灵的”“好心动”“嘴挺好看的”“这状态真不错”必须失败。纯视觉 source 中约见问题必须 context_drift。
惩罚甜宠卖萌、空泛夸赞、干净模板腔、同义复读和夜课/课程错场；具体经历和服务事实仍需 source 依据。
pass 时 codes 必须是空数组；fail 时 codes 只能从 unnatural_language、unsupported_detail、context_drift、ai_tone、repetitive、cutesy_tone、generic_praise、too_clean 中选择。
只输出 JSON：{"results":[{"slot":"原槽位","decision":"pass或fail","confidence":0.0,"codes":[]}]}。"""
PRODUCT_REVIEW_SYSTEM = """你是严格的中文成人用品评论质量审查员。pass 必须围绕 source 中的实体产品，像真实买家短问或短评，所问功能必须适合该产品。
允许问尺寸、材质、软硬、力度、档位、噪音、续航、防水、清洁、价格和隐私包装；问题可以询问 source 尚未给答案的细节，这不是 unsupported_detail。“硅胶软不软”“十档差别大吗”“十档也太狠了”“静音这个实用”应高置信 pass；同组重复同一属性时后出现者必须以 repetitive fail。参数残片、虚构时长、配件材质必须 fail。禁止一次堆两个问题、编已购买/使用经历、人物外观夸赞、约见、课程和纯色情反应。
pass 时 codes=[]；fail codes 从 irrelevant_feature、fake_experience、visual_drift、generic_praise、ai_tone、repetitive、unsupported_detail 中选择。只输出 JSON：{"results":[{"slot":"原槽位","decision":"pass或fail","confidence":0.0,"codes":[]}]}。"""
ADULT_CONTEXT_DRIFT = ("夜课", "课程", "教什么", "辅导员", "积分", "签到", "排名", "活动")
ADULT_AI_DRIFT = ("具体是", "多长时间", "然后呢", "后来", "后面", "继续", "怎么拍", "光泽感", "看完了", "缓", "好水", "想看", "会不会", "会怎样", "深入")
ADULT_CUTESY_MARKERS = ("软软的", "水灵灵", "心动", "挺好看", "真不错", "好亲")
PRODUCT_DRIFT_MARKERS = ("今晚有空", "能约", "真美", "水滋滋", "馋了", "硬了", "我用过", "买过", "用过", "够玩一年", "看着不错", "听着带劲", "应该")
ADULT_STYLE_EXAMPLES = ("真美", "水滋滋", "好润", "好嫩好润", "想摸下", "欲望上来了", "这谁顶得住啊", "够带劲", "晚上能约吗", "馋了")

SYSTEM_PROMPT = """你在模拟同一个 Telegram 页面下互不认识的普通成年用户反应，不是写手，也不是运营。
目标是整组像真人随手敲出来：有人只回两三个字，有人抓一个具体词，有人问一个小问题，有人轻轻调侃。
逐槽严格服从 speech_act、length 和 punctuation；每条只完成一个动作，不要把每条都写成完整观点。
禁止精致点评、总结原文、广告腔、文学化扩写、统一句式和强行加“哈哈”。
禁止使用：画面感、太形象、太生动、氛围感、让人、仿佛、值得、确实、看起来、感觉、这个内容、这波操作、感谢分享。
只使用 source 已有事实，不编已经发生的经历、交易或服务；禁止未成年人和联系方式；信息不足时宁可短反应。
不要编号，不要解释。严格输出 JSON：{"reactions":[{"slot":"原样返回brief中的slot","content":"短句"}]}。"""

def _briefs(scene: Scene) -> tuple[dict[str, Any], ...]:
    style = os.getenv("AI_POPULATION_STYLE")
    base = ADULT_PRODUCT_BRIEFS if style == "adult_product" else ADULT_VISUAL_BRIEFS if style == "adult_visual" else BRIEFS
    if not scene.voice_profiles:
        return base
    return tuple({**brief, "voice_profile": scene.voice_profiles[index]} for index, brief in enumerate(base))

def _system_prompt(scene: Scene) -> str:
    style = os.getenv("AI_POPULATION_STYLE")
    prompt = SYSTEM_PROMPT + (ADULT_PRODUCT_SYSTEM if style == "adult_product" else ADULT_VISUAL_SYSTEM if style == "adult_visual" else "")
    return prompt + (VOICE_PROFILE_SYSTEM if scene.voice_profiles else "")

def _latest_scene(session, task_type: str, offset: int = 0) -> Scene | None:
    case_filter = os.getenv("AI_POPULATION_CONTEXT_HASH", "")
    if task_type == "channel_comment" and os.getenv("AI_POPULATION_STYLE") == "adult_product" and case_filter:
        rows = session.scalars(select(ChannelMessage).where(ChannelMessage.tenant_id == TENANT_ID).order_by(ChannelMessage.published_at.desc()).limit(10000))
        for row in rows:
            context = str(row.content_preview or "").strip()
            if hashlib.sha256(context.encode()).hexdigest()[:12] == case_filter:
                return Scene("comment", case_filter, _safe_context("comment", context))
    tasks = list(session.scalars(
        select(Task).where(
            Task.tenant_id == TENANT_ID,
            Task.type == task_type,
            Task.deleted_at.is_(None),
        ).order_by(Task.updated_at.desc()).limit(20)
    ))
    if not tasks:
        return None
    task_ids = [task.id for task in tasks]
    action_type = "post_comment" if task_type == "channel_comment" else "send_message"
    actions = session.scalars(
        select(Action).where(
            Action.tenant_id == TENANT_ID,
            Action.task_id.in_(task_ids),
            Action.action_type == action_type,
            Action.status == "success",
        ).order_by(Action.created_at.desc()).limit(MAX_ACTION_ROWS)
    )
    valid_index = 0
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        key = "message_content" if task_type == "channel_comment" else "ai_generation_history"
        context = str(payload.get(key) or "").strip()
        if context:
            context_hash = hashlib.sha256(context.encode()).hexdigest()[:12]
            if case_filter and context_hash != case_filter:
                continue
            kind = "comment" if task_type == "channel_comment" else "group"
            case_id = context_hash
            safe_context = _safe_context(kind, context)
            if safe_context:
                if valid_index < offset:
                    valid_index += 1
                    continue
                return Scene(kind, case_id, safe_context)
    return None

def _safe_context(kind: str, context: str) -> str:
    if kind == "group":
        return "\n".join(sanitize_group_messages(context.splitlines())[-5:])
    compact = re.sub(r"https?://\S+|@[A-Za-z0-9_]+|\b\d{6,}\b", "", context)
    clauses = safe_clauses(compact)
    return "\n".join(clauses[-3:])[:300]

def _voice_payload(row: AiAccountVoiceProfile) -> dict[str, Any]:
    contract = voice_contract_v3(row)
    contract.pop("colloquial_markers", None)
    contract.pop("summary", None)
    return contract

def _load_voice_profiles(session, task_type: str) -> tuple[dict[str, Any], ...]:
    action_type = "post_comment" if task_type == "channel_comment" else "send_message"
    account_ids = list(dict.fromkeys(session.scalars(
        select(Action.account_id).where(
            Action.tenant_id == TENANT_ID,
            Action.task_type == task_type,
            Action.action_type == action_type,
            Action.status == "success",
            Action.account_id.is_not(None),
        ).order_by(Action.created_at.desc()).limit(500)
    )))
    rows = session.scalars(select(AiAccountVoiceProfile).where(
        AiAccountVoiceProfile.tenant_id == TENANT_ID,
        AiAccountVoiceProfile.account_id.in_(account_ids),
        AiAccountVoiceProfile.status == "active",
        AiAccountVoiceProfile.quality_status == "active",
    ))
    latest: dict[int, AiAccountVoiceProfile] = {}
    for row in rows:
        if row.account_id not in latest or row.version > latest[row.account_id].version:
            latest[row.account_id] = row
    selected: list[dict[str, Any]] = []
    for account_id in account_ids:
        if account_id not in latest:
            continue
        payload = _voice_payload(latest[account_id])
        selected.append(payload)
        if len(selected) == len(BRIEFS):
            return tuple(selected)
    raise RuntimeError(f"voice_profile_shortfall:{len(selected)}/{len(BRIEFS)}")


def _credentials(session) -> tuple[tuple[AiProviderCredentials, ...], AiProviderCredentials]:
    rows = [session.get(AiProvider, provider_id) for provider_id in (*REVIEWER_PROVIDER_IDS, MIMO_PROVIDER_ID)]
    if any(row is None for row in rows):
        raise RuntimeError("shadow_provider_missing")
    keys = tuple(decrypt_secret(row.api_key_ciphertext) for row in rows)
    if not all(keys):
        raise RuntimeError("shadow_provider_key_missing")
    credentials = tuple(
        AiProviderCredentials(row.provider_name, row.provider_type, row.base_url, row.model_name, key, row.api_key_header)
        for row, key in zip(rows, keys, strict=True)
    )
    return credentials[:-1], credentials[-1]


def _extract(payload: Any) -> list[dict[str, str]]:
    items = payload.get("reactions") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("reactions_missing")
    result: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        slot = str(item.get("slot") or "").strip()
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        if slot and content:
            result.append({"slot": slot, "content": content[:40]})
    return result


def _metrics(items: list[dict[str, str]], scene: Scene) -> dict[str, Any]:
    contents = [item["content"] for item in items]
    lengths = [len(content) for content in contents]
    return {
        "count": len(contents),
        "mean_length": round(statistics.mean(lengths), 2) if lengths else None,
        "micro_rate": round(sum(length <= 5 for length in lengths) / len(lengths), 3) if lengths else None,
        "no_terminal_punctuation_rate": round(
            sum(content[-1:] not in "。！？!?" for content in contents) / len(contents), 3
        ) if contents else None,
        "violations": [] if len(contents) == len(_briefs(scene)) else ["wrong_count"],
    }


def _slot_violations(
    scene: Scene,
    item: dict[str, str],
    brief: dict[str, str],
    accepted: list[dict[str, str]],
) -> list[str]:
    content = item.get("content", "")
    lower, upper = map(int, brief["length"].split("-"))
    failures: list[str] = []
    if item.get("slot") != brief["slot"]:
        failures.append("slot_mismatch")
    if not max(1, lower - 1) <= len(content) <= upper + 2:
        failures.append("length_band_mismatch")
    if any(marker in content for marker in (*POLISHED_MARKERS, *UNSUPPORTED_MARKERS)):
        failures.append("forbidden_marker")
    if os.getenv("AI_POPULATION_STYLE") == "adult_visual" and any(marker in content for marker in ADULT_CONTEXT_DRIFT):
        failures.append("adult_context_drift")
    if os.getenv("AI_POPULATION_STYLE") == "adult_visual" and any(marker in content for marker in ADULT_AI_DRIFT):
        failures.append("adult_ai_drift")
    if os.getenv("AI_POPULATION_STYLE") == "adult_visual" and (any(marker in content for marker in ADULT_CUTESY_MARKERS) or re.search(r"(嘴|唇).{0,3}软", content)):
        failures.append("adult_cutesy_tone")
    if os.getenv("AI_POPULATION_STYLE") == "adult_product" and any(marker in content for marker in PRODUCT_DRIFT_MARKERS):
        failures.append("product_visual_drift")
    if content in ADULT_STYLE_EXAMPLES and sum(row["content"] in ADULT_STYLE_EXAMPLES for row in accepted) >= 2:
        failures.append("style_example_overcopy")
    if content.rstrip("。！？!?~～").removesuffix("的") in {row["content"].rstrip("。！？!?~～").removesuffix("的") for row in accepted}:
        failures.append("exact_duplicate")
    if "谁" in content and "得住" in content and any("谁" in row["content"] and "得住" in row["content"] for row in accepted):
        failures.append("template_duplicate")
    if brief["punctuation"] == "none" and content[-1:] in "。！？!?":
        failures.append("terminal_punctuation_forbidden")
    return failures


def _accept_micro_items(
    scene: Scene,
    items: list[dict[str, str]],
    briefs: tuple[dict[str, str], ...],
    accepted: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, list[str]]]:
    by_slot = {item["slot"]: item for item in items}
    additions: list[dict[str, str]] = []
    failures: dict[str, list[str]] = {}
    for brief in briefs:
        item = by_slot.get(brief["slot"], {"slot": "", "content": ""})
        violations = _slot_violations(scene, item, brief, [*accepted, *additions])
        if violations:
            failures[brief["slot"]] = violations
        else:
            additions.append(item)
    return additions, failures


def _generate_micro_batch(
    scene: Scene,
    briefs: tuple[dict[str, str], ...],
    accepted: list[dict[str, str]],
    credentials: AiProviderCredentials,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], int]:
    attempts: list[dict[str, Any]] = []
    total_tokens = 0
    pending = list(briefs)
    collected: list[dict[str, str]] = []
    for attempt in range(1, MAX_SLOT_ATTEMPTS + 1):
        prompt = json.dumps({
            "scene": scene.kind,
            "source": scene.context,
            "briefs": pending,
            "already_accepted": [row["content"] for row in [*accepted, *collected]],
            "previous_rejection": attempts[-1]["violations"] if attempts else [],
        }, ensure_ascii=False, separators=(",", ":"))
        try:
            payload, usage = ai_gateway.generate_structured(
                credentials, prompt, temperature=0.95, max_tokens=3072,
                system_prompt=_system_prompt(scene), timeout=REQUEST_TIMEOUT_SECONDS,
            )
            total_tokens += int(usage.total_tokens or 0)
            items = _extract(payload)
            additions, violations = _accept_micro_items(scene, items, tuple(pending), [*accepted, *collected])
        except Exception as exc:  # noqa: BLE001
            additions = []
            violations = {brief["slot"]: [type(exc).__name__] for brief in pending}
        collected.extend(additions)
        pending = [brief for brief in pending if brief["slot"] in violations]
        attempts.append({"attempt": attempt, "violations": violations})
        if not pending:
            break
    by_slot = {item["slot"]: item for item in collected}
    return [by_slot[brief["slot"]] for brief in briefs if brief["slot"] in by_slot], attempts, total_tokens


def _generate_micro_batches(scene: Scene, label: str, credentials: AiProviderCredentials) -> dict[str, Any]:
    started = time.monotonic()
    accepted: list[dict[str, str]] = []
    attempt_log: dict[str, list[dict[str, Any]]] = {}
    total_tokens = 0
    scene_briefs = _briefs(scene)
    for start in range(0, len(scene_briefs), MICRO_BATCH_SIZE):
        briefs = scene_briefs[start:start + MICRO_BATCH_SIZE]
        items, attempts, tokens = _generate_micro_batch(scene, briefs, accepted, credentials)
        attempt_log[f"{start + 1}-{start + len(briefs)}"] = attempts
        accepted.extend(items)
        total_tokens += tokens
    return {
        "model": label,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "total_tokens": total_tokens,
        "metrics": _metrics(accepted, scene),
        "reactions": accepted,
        "attempts": attempt_log,
    }


def _semantic_review(
    scene: Scene,
    items: list[dict[str, str]],
    credentials: tuple[AiProviderCredentials, ...],
) -> tuple[dict[str, list[str]], int, str, list[str]]:
    prompt = json.dumps({"source": scene.context, "candidates": items}, ensure_ascii=False, separators=(",", ":"))
    errors: list[str] = []
    for credential in credentials:
        try:
            payload, usage = ai_gateway.generate_structured(
                credential, prompt, temperature=0.1, max_tokens=3072,
                system_prompt=PRODUCT_REVIEW_SYSTEM if os.getenv("AI_POPULATION_STYLE") == "adult_product" else SEMANTIC_REVIEW_SYSTEM,
            )
            reviewer_name = credential.provider_name
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{credential.provider_name}:{type(exc).__name__}")
    else:
        raise RuntimeError(f"semantic_review_providers_failed:{','.join(errors)}")
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("semantic_review_results_missing")
    by_slot = {str(row.get("slot")): row for row in rows if isinstance(row, dict)}
    failures: dict[str, list[str]] = {}
    for brief in _briefs(scene):
        row = by_slot.get(brief["slot"])
        if row is None:
            failures[brief["slot"]] = ["review_missing"]
            continue
        decision = str(row.get("decision") or "")
        confidence = float(row.get("confidence") or 0)
        codes = [str(code) for code in (row.get("codes") or []) if str(code)]
        if decision != "pass" or confidence < SEMANTIC_REVIEW_CONFIDENCE or codes:
            failures[brief["slot"]] = codes or ["review_uncertain"]
    return failures, int(usage.total_tokens or 0), reviewer_name, errors


def _semantic_retry(
    scene: Scene,
    items: list[dict[str, str]],
    failures: dict[str, list[str]],
    credentials: AiProviderCredentials,
) -> tuple[list[dict[str, str]], list[dict[str, Any]], int]:
    retry_briefs = tuple(
        {**brief, "semantic_feedback": failures[brief["slot"]]}
        for brief in _briefs(scene) if brief["slot"] in failures
    )
    kept = [item for item in items if item["slot"] not in failures]
    replacements, attempts, tokens = _generate_micro_batch(scene, retry_briefs, kept, credentials)
    by_slot = {item["slot"]: item for item in [*kept, *replacements]}
    ordered = [by_slot[brief["slot"]] for brief in _briefs(scene) if brief["slot"] in by_slot]
    return ordered, attempts, tokens


def _generate_reviewed(
    scene: Scene,
    generator: AiProviderCredentials,
    reviewers: tuple[AiProviderCredentials, ...],
) -> dict[str, Any]:
    started = time.monotonic()
    result = _generate_micro_batches(scene, "MiMo-v2.5", generator)
    items = list(result["reactions"])
    total_tokens = int(result["total_tokens"])
    review_log: list[dict[str, Any]] = []
    failures: dict[str, list[str]] = {}
    for round_no in range(1, SEMANTIC_REVIEW_ROUNDS + 1):
        try:
            failures, review_tokens, reviewer_name, errors = _semantic_review(scene, items, reviewers)
        except RuntimeError as exc:
            failures = {"*": ["review_blocked"]}
            review_log.append({"round": round_no, "reviewer": "", "provider_errors": [str(exc)], "failures": failures})
            break
        total_tokens += review_tokens
        review_log.append({"round": round_no, "reviewer": reviewer_name, "provider_errors": errors, "failures": failures})
        if not failures or round_no == SEMANTIC_REVIEW_ROUNDS:
            break
        items, attempts, retry_tokens = _semantic_retry(scene, items, failures, generator)
        result["attempts"][f"semantic_retry_{round_no}"] = attempts
        total_tokens += retry_tokens
    metrics = _metrics(items, scene)
    if failures:
        metrics["violations"].append("semantic_review_failed")
    return {
        **result, "duration_ms": round((time.monotonic() - started) * 1000),
        "total_tokens": total_tokens, "metrics": metrics, "reactions": items,
        "semantic_review_pass": not failures, "semantic_reviews": review_log,
    }


def main() -> None:
    scene_filter = os.getenv("AI_POPULATION_SCENE", "")
    scene_offset = max(0, int(os.getenv("AI_POPULATION_SCENE_OFFSET", "0")))
    with SessionLocal() as session:
        scenes = [scene for scene in (
            _latest_scene(session, "group_ai_chat", scene_offset),
            _latest_scene(session, "channel_comment", scene_offset),
        ) if scene and scene.context and (not scene_filter or scene.kind == scene_filter)]
        scenes = [replace(
            scene,
            voice_profiles=_load_voice_profiles(
                session, "channel_comment" if scene.kind == "comment" else "group_ai_chat"
            ),
        ) for scene in scenes]
        reviewers, mimo = _credentials(session)
    results = []
    for scene in scenes:
        models = [_generate_reviewed(
            scene, replace(mimo, model_name="mimo-v2.5"),
            reviewers,
        )]
        results.append({
            "scene": scene.kind,
            "case_id": scene.case_id,
            "voice_profiles_bound": len(scene.voice_profiles),
            "voice_profile_variants": len({json.dumps(row, sort_keys=True) for row in scene.voice_profiles}),
            "models": models,
        })
    print(json.dumps({
        "AI_POPULATION_SHADOW": results,
        "writes_database": False,
        "sends_telegram": False,
        "prints_source_context": False,
    }, ensure_ascii=False, sort_keys=True))

if __name__ == "__main__":
    main()
