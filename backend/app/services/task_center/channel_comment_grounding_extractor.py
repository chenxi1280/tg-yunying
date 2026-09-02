from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


EXTRACTOR_VERSION = "channel_comment_grounding_extractor_v1"
GROUNDING_CONTRACT_VERSION = "channel_comment_grounding_v1"
GROUNDING_POLICY_VERSION = "channel_comment_grounding_policy_v1"
SEMANTIC_CAPACITY_POLICY_VERSION = "channel_comment_semantic_capacity_v1"
SPEECH_ACTS = (
    "reaction", "specific_question", "cautious_verification", "concise_agreement",
)
NEGATION_MARKERS = ("不是", "不要找", "并非", "别找", "非")
TEACHER_NEGATION_INTRODUCERS = ("不要找", "不是", "并非", "别找")
TEACHER_NAME_INTRODUCERS = (
    "今日主推",
    "今天推荐",
    "朋友推荐",
    "我觉得",
    "听说",
    "据说",
    "今天的",
    "今日的",
    "昨天的",
    "今晚的",
    "推荐",
    "主推",
    "觉得",
    "新人",
)
TEACHER_SUFFIX_PATTERN = re.compile(r"(?P<name>[\w\u4e00-\u9fff]{1,12})老师")
TEACHER_FIELD_PATTERN = re.compile(
    r"(?:姓名|推荐|今日主推)[：:\s]+(?P<name>[\w\u4e00-\u9fff·]{1,12})(?:老师)?",
)
URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+|@\w+", re.IGNORECASE)
TEMPORAL_PATTERN = re.compile(r"今日|今天|当天|下午|今晚|今夜")
ASPECT_PATTERNS = (
    ("body_feature", re.compile(r"身高|腿长|比例|腰细|高挑|身材|罩杯|胸围|胸大|丰满|苗条|骨架|肉感|微胖|1[5678]\d(?:cm)?")),
    ("appearance_style", re.compile(r"颜值|气质|好看|漂亮|甜美|御姐|可爱|清纯|萝莉|名媛|极品|少妇")),
    ("outfit_feature", re.compile(r"黑丝|白丝|肉丝|制服|护士|空姐|穿搭|造型|cos|女仆|旗袍|高跟", re.IGNORECASE)),
    ("service_feature", re.compile(r"水疗|按摩|SPA|服务|项目|手法|配合度|口活|漫游|不机车|不催钟|态度", re.IGNORECASE)),
    ("price_cost", re.compile(r"课费|价格|多少钱|收费|预算|性价比|定金|车费|折后|全包|\d+/[pP]|\d+[pP]")),
    ("score_rating", re.compile(r"评分|综合|好评|中评|差评|体验|战报|推荐榜|验证榜|上榜|打分")),
    ("location_booking", re.compile(r"(?:天河|越秀|海珠|白云|番禺|南山|福田|罗湖|宝安|龙华|龙岗|朝阳|海淀|丰台|西城|东城|武侯|锦江|成华|青羊|高新|金水|二七|管城|中原|惠济|郑东|小寨|雁塔|碑林|南稍门|和平|滨江道|南开|河西|河东|公寓|酒店|到店|开课|排课|档期|在课|可约|预约|预订)")),
    ("authenticity", re.compile(r"素颜|真照|原图|修图|照骗|实拍|本人|真实|探路|测评|避坑|踩雷")),
    ("promotion", re.compile(r"活动|优惠|特惠|折扣|立减|福利")),
    ("time_window", TEMPORAL_PATTERN),
)


def extract_grounding_facts(
    source_text: str,
    source_published_at: datetime,
    *,
    content_route: str,
    timezone_name: str = "Asia/Shanghai",
) -> dict:
    teachers = _teacher_candidates(source_text)
    evidence = _aspect_evidence(
        source_text,
        teachers=teachers,
        source_published_at=source_published_at,
        timezone_name=timezone_name,
    )
    teacher_state = _teacher_state(teachers)
    source_state = _source_state(source_text, evidence, content_route=content_route)
    variants = _semantic_variants(teachers, evidence)
    blocks = _evidence_blocks(source_text, teachers, evidence)
    return {
        "source_state": source_state,
        "teacher_state": teacher_state,
        "teacher_candidates_json": teachers,
        "aspect_evidence_json": evidence,
        "evidence_blocks_json": blocks,
        "semantic_variant_units_json": variants,
        "groundable_capacity_count": len(variants),
        "extraction_audit_json": {
            "extractor_version": EXTRACTOR_VERSION,
            "source_length": len(source_text),
            "source_content_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "content_route": content_route,
            "teacher_candidate_count": len(teachers),
            "evidence_count": len(evidence),
            "variant_count": len(variants),
            "span_unit": "unicode_code_point_half_open",
        },
    }


def _teacher_candidates(source_text: str) -> list[dict]:
    raw = [
        *_teacher_matches(source_text, TEACHER_SUFFIX_PATTERN, "explicit_teacher_suffix"),
        *_teacher_matches(source_text, TEACHER_FIELD_PATTERN, "explicit_teacher_field"),
    ]
    ordered = sorted(raw, key=lambda row: (row["source_start"], row["source_end"]))
    unique = []
    seen = set()
    for row in ordered:
        identity = (row["source_start"], row["source_end"], row["normalized_name"])
        if identity in seen:
            continue
        seen.add(identity)
        unique.append({**row, "candidate_id": f"teacher-{len(unique) + 1}"})
    return unique


def _teacher_matches(source_text: str, pattern: re.Pattern, name_kind: str) -> list[dict]:
    rows = []
    for match in pattern.finditer(source_text):
        name = match.group("name")
        start, end = match.span("name")
        name, start, negated_in_name = _clean_teacher_name(name, start)
        normalized = name.removesuffix("老师")
        if not normalized:
            continue
        negated = negated_in_name or _is_negated(source_text, match.start())
        rows.append({
            "display_name": f"{normalized}老师",
            "normalized_name": normalized,
            "name_kind": name_kind,
            "evidence_ids": [],
            "source_text": source_text[start:end],
            "source_start": start,
            "source_end": end,
            "confidence": "high",
            "negated": negated,
            "attribute_evidence_ids": [],
        })
    return rows


def _clean_teacher_name(name: str, start: int) -> tuple[str, int, bool]:
    name, start, negated = _strip_rightmost_introducer(
        name,
        start,
        TEACHER_NEGATION_INTRODUCERS,
    )
    name, start, _introduced = _strip_rightmost_introducer(
        name,
        start,
        TEACHER_NAME_INTRODUCERS,
    )
    while name.startswith("的"):
        name = name[1:]
        start += 1
    return name, start, negated


def _strip_rightmost_introducer(
    name: str,
    start: int,
    markers: tuple[str, ...],
) -> tuple[str, int, bool]:
    matches = [
        (name.rfind(marker), marker)
        for marker in markers
        if name.rfind(marker) >= 0
    ]
    if not matches:
        return name, start, False
    marker_at, marker = max(matches, key=lambda item: item[0] + len(item[1]))
    shift = marker_at + len(marker)
    return name[shift:], start + shift, True


def _is_negated(source_text: str, start: int) -> bool:
    prefix = source_text[max(0, start - 6):start]
    return any(marker in prefix for marker in NEGATION_MARKERS)


def _aspect_evidence(
    source_text: str,
    *,
    teachers: list[dict],
    source_published_at: datetime,
    timezone_name: str,
) -> list[dict]:
    conflicted_names = _conflicted_teacher_names(teachers)
    matches = [
        (
            int(row["source_start"]),
            int(row["source_end"]),
            "teacher_identity",
            str(row["source_text"]),
        )
        for row in teachers
        if not row["negated"] and row["normalized_name"] not in conflicted_names
    ]
    for code, pattern in ASPECT_PATTERNS:
        for match in pattern.finditer(source_text):
            matches.append((match.start(), match.end(), code, match.group(0)))
    matches.sort(key=lambda item: (item[0], item[1], item[2]))
    evidence = []
    seen = set()
    for start, end, code, excerpt in matches:
        identity = (start, end, code)
        if identity in seen:
            continue
        seen.add(identity)
        evidence.append(_evidence_row(
            len(evidence) + 1,
            source_text,
            start=start,
            end=end,
            code=code,
            excerpt=excerpt,
            teacher_id=_teacher_for_span(source_text, teachers, start),
            source_published_at=source_published_at,
            timezone_name=timezone_name,
        ))
    _bind_teacher_evidence(teachers, evidence)
    return evidence


def _evidence_row(
    ordinal: int,
    source_text: str,
    *,
    start: int,
    end: int,
    code: str,
    excerpt: str,
    teacher_id: str,
    source_published_at: datetime,
    timezone_name: str,
) -> dict:
    temporal = bool(TEMPORAL_PATTERN.search(excerpt))
    return {
        "evidence_id": f"e-{ordinal}",
        "aspect_code": code,
        "source_text": source_text[start:end],
        "normalized_value": excerpt.casefold(),
        "source_start": start,
        "source_end": end,
        "polarity": "supported",
        "teacher_candidate_id": teacher_id,
        "time_validity": "same_local_day" if temporal else "timeless",
        "valid_until": (
            _local_day_end(source_published_at, timezone_name).isoformat()
            if temporal else None
        ),
    }


def _teacher_for_span(source_text: str, teachers: list[dict], start: int) -> str:
    conflicts = _conflicted_teacher_names(teachers)
    positive = [
        row for row in teachers
        if not row["negated"] and row["normalized_name"] not in conflicts
    ]
    line_start = source_text.rfind("\n", 0, start) + 1
    same_block = [
        row for row in positive
        if line_start <= int(row["source_start"]) <= start
    ]
    if same_block:
        owner = max(same_block, key=lambda row: int(row["source_start"]))
        return str(owner["candidate_id"])
    distinct_names = {str(row["normalized_name"]) for row in teachers}
    return (
        str(positive[0]["candidate_id"])
        if len(positive) == 1 and len(distinct_names) == 1
        else ""
    )


def _conflicted_teacher_names(teachers: list[dict]) -> set[str]:
    polarity: dict[str, set[bool]] = {}
    for row in teachers:
        polarity.setdefault(str(row["normalized_name"]), set()).add(bool(row["negated"]))
    return {name for name, values in polarity.items() if values == {False, True}}


def _bind_teacher_evidence(teachers: list[dict], evidence: list[dict]) -> None:
    for teacher in teachers:
        teacher["evidence_ids"] = [
            row["evidence_id"] for row in evidence
            if row["teacher_candidate_id"] == teacher["candidate_id"]
        ]
        teacher["attribute_evidence_ids"] = list(teacher["evidence_ids"])


def _teacher_state(teachers: list[dict]) -> str:
    positive = [row for row in teachers if not row["negated"]]
    states: dict[str, set[bool]] = {}
    for row in teachers:
        states.setdefault(str(row["normalized_name"]), set()).add(bool(row["negated"]))
    if any(values == {False, True} for values in states.values()):
        return "conflict"
    if not positive:
        return "none"
    return "multiple_supported" if len(positive) > 1 else "explicit_supported"


def _source_state(source_text: str, evidence: list[dict], *, content_route: str) -> str:
    if not content_route:
        return "route_conflict"
    without_links = URL_PATTERN.sub("", source_text).strip()
    if not without_links or not evidence:
        return "insufficient"
    return "minimal" if len(evidence) == 1 else "ready"


def _semantic_variants(teachers: list[dict], evidence: list[dict]) -> list[dict]:
    conflicts = _conflicted_teacher_names(teachers)
    teacher_by_id = {
        row["candidate_id"]: row
        for row in teachers
        if not row["negated"] and row["normalized_name"] not in conflicts
    }
    variants = []
    for speech_act_index, _ in enumerate(SPEECH_ACTS):
        for evidence_index, row in enumerate(evidence):
            effective_speech_act = SPEECH_ACTS[
                (speech_act_index + evidence_index) % len(SPEECH_ACTS)
            ]
            teacher = teacher_by_id.get(row["teacher_candidate_id"])
            variants.append({
                "variant_id": f"{row['evidence_id']}:{effective_speech_act}",
                "primary_evidence_id": row["evidence_id"],
                "secondary_evidence_id": "",
                "aspect_code": row["aspect_code"],
                "aspect_text": row["source_text"],
                "teacher_candidate_id": row["teacher_candidate_id"],
                "teacher_name": teacher["display_name"] if teacher else "",
                "speech_act": effective_speech_act,
            })
    return variants


def _evidence_blocks(
    source_text: str,
    teachers: list[dict],
    evidence: list[dict],
) -> list[dict]:
    blocks = []
    offset = 0
    for line in source_text.splitlines(keepends=True) or [source_text]:
        end = offset + len(line)
        evidence_ids = [
            row["evidence_id"] for row in evidence
            if offset <= int(row["source_start"]) < end
        ]
        teacher_ids = [
            row["candidate_id"] for row in teachers
            if offset <= int(row["source_start"]) < end
        ]
        if evidence_ids or teacher_ids:
            blocks.append({
                "block_id": f"block-{len(blocks) + 1}",
                "source_start": offset,
                "source_end": end,
                "excerpt": source_text[offset:end],
                "teacher_candidate_ids": teacher_ids,
                "evidence_ids": evidence_ids,
            })
        offset = end
    return blocks


def _local_day_end(value: datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    local = value.astimezone(zone)
    return datetime.combine(local.date(), time.max, tzinfo=zone)


def stable_grounding_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "EXTRACTOR_VERSION",
    "GROUNDING_CONTRACT_VERSION",
    "GROUNDING_POLICY_VERSION",
    "SEMANTIC_CAPACITY_POLICY_VERSION",
    "extract_grounding_facts",
    "stable_grounding_hash",
]
