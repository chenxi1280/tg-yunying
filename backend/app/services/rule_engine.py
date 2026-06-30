from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from sqlalchemy.orm import Session

from app.models import RuleSet, RuleSetVersion, Task


TASK_TYPE_LABELS = {
    "group_relay": "监听转发",
    "group_ai_chat": "AI 回复",
    "channel_comment": "AI 评论",
    "message_send": "普通消息发送",
    "channel_view": "频道浏览",
    "channel_like": "频道点赞",
}
RULE_BINDING_REQUIRED_TASK_TYPES = frozenset({"group_relay", "group_ai_chat", "channel_comment"})
RULE_BINDING_REQUIRED_MESSAGE = "任务必须绑定已发布规则集版本"


@dataclass
class RuleCheckResult:
    passed: bool
    reason: str = ""
    hits: list[str] = field(default_factory=list)


@dataclass
class OutputPolicyResult:
    allowed: bool
    content: str
    reason: str = ""
    action: str = "pass"
    transformed: bool = False
    hits: list[str] = field(default_factory=list)


def bound_rule_version(session: Session, task: Task) -> RuleSetVersion | None:
    config = task.type_config or {}
    version_id = _as_int(config.get("rule_set_version_id"))
    if version_id:
        version = session.get(RuleSetVersion, version_id)
        if version and version.tenant_id == task.tenant_id:
            if version.status == "draft":
                task.last_error = "绑定的规则版本尚未发布"
                return None
            return version
        task.last_error = "绑定的规则版本不存在"
        return None
    rule_set_id = _as_int(config.get("rule_set_id"))
    if not rule_set_id:
        if task.type in RULE_BINDING_REQUIRED_TASK_TYPES:
            task.last_error = RULE_BINDING_REQUIRED_MESSAGE
        return None
    rule_set = session.get(RuleSet, rule_set_id)
    if not rule_set or rule_set.tenant_id != task.tenant_id:
        task.last_error = "绑定的规则集不存在"
        return None
    if not rule_set.active_version_id:
        task.last_error = "绑定的规则集没有已发布版本"
        return None
    version = session.get(RuleSetVersion, rule_set.active_version_id)
    if not version or version.tenant_id != task.tenant_id or version.rule_set_id != rule_set.id:
        task.last_error = "绑定的活动规则版本不存在"
        return None
    if version.status != "published":
        task.last_error = "绑定的活动规则版本不是已发布状态"
        return None
    return version


def task_type_labels(task_types: Any) -> list[str]:
    values = _as_str_list(task_types)
    return [TASK_TYPE_LABELS.get(value, value) for value in values]


def evaluate_input_filter(content: str, sender_id: str = "", message_type: str = "text", filters: dict[str, Any] | None = None) -> RuleCheckResult:
    filters = filters or {}
    text = content or ""
    lowered = text.lower()
    hits: list[str] = []
    whitelist = [item for item in _as_str_list(filters.get("keyword_whitelist")) if item]
    if whitelist:
        matched = [item for item in whitelist if item.lower() in lowered]
        if not matched:
            return RuleCheckResult(False, f"未命中白名单关键词：{', '.join(whitelist[:5])}", hits)
        hits.extend(f"白名单:{item}" for item in matched[:5])
    blacklist = [item for item in _as_str_list(filters.get("keyword_blacklist")) if item]
    blocked = [item for item in blacklist if item.lower() in lowered]
    if blocked:
        return RuleCheckResult(False, f"命中黑名单关键词：{', '.join(blocked[:5])}", [*hits, *(f"黑名单:{item}" for item in blocked[:5])])
    min_len = _as_int(filters.get("min_message_length"))
    if min_len is not None and len(text) < min_len:
        return RuleCheckResult(False, f"内容长度低于最小值 {min_len}", hits)
    max_len = _as_int(filters.get("max_message_length"))
    if max_len is not None and len(text) > max_len:
        return RuleCheckResult(False, f"内容长度超过最大值 {max_len}", hits)
    if sender_id and str(sender_id) in set(_as_str_list(filters.get("blocked_user_ids"))):
        return RuleCheckResult(False, f"发送者 {sender_id} 在屏蔽列表", hits)
    allowed_media = set(_as_str_list(filters.get("allowed_media_types")))
    if allowed_media and message_type not in allowed_media:
        return RuleCheckResult(False, f"消息类型 {message_type or 'text'} 不在允许列表", hits)
    is_text = message_type in {"text", "文本", ""}
    if filters.get("only_with_media") and is_text:
        return RuleCheckResult(False, "规则要求带媒体消息", hits)
    if filters.get("only_text") and not is_text:
        return RuleCheckResult(False, "规则只允许文本消息", hits)
    expression = filters.get("expression")
    if expression and not _passes_expression(text, sender_id, message_type, expression):
        return RuleCheckResult(False, _expression_reason(text, sender_id, message_type, expression), hits)
    return RuleCheckResult(True, "", hits or ["默认通过"])


def transform_content(content: str, transforms: dict[str, Any] | None = None) -> str:
    transforms = transforms or {}
    text = content or ""
    link_pattern = r"https?://\S+|t\.me/\S+"
    if transforms.get("remove_mentions"):
        text = re.sub(r"@\w+", "", text)
    if transforms.get("remove_links"):
        text = re.sub(link_pattern, "", text)
    replacement = transforms.get("replace_links")
    if replacement is not None:
        if isinstance(replacement, dict):
            text = re.sub(link_pattern, lambda match: str(replacement.get(match.group(0), replacement.get("*", ""))), text)
        else:
            text = re.sub(link_pattern, str(replacement), text)
    for keyword in _as_configured_str_list(transforms.get("delete_keywords") or transforms.get("remove_keywords")):
        if keyword:
            text = re.sub(re.escape(keyword), "", text, flags=re.IGNORECASE)
    replacements = transforms.get("keyword_replacements") or transforms.get("replace_keywords") or {}
    if isinstance(replacements, dict):
        for source, target in replacements.items():
            text = text.replace(str(source), str(target))
    if transforms.get("strip_source_attribution"):
        text = re.sub(r"(?m)^\s*(来源|转自|via)[:：].*$", "", text)
    max_length = _as_int(transforms.get("max_length") or transforms.get("trim_to_length"))
    if max_length and len(text) > max_length:
        text = text[:max_length].rstrip()
    prefix = str(transforms.get("prefix") or "")
    suffix = str(transforms.get("suffix") or "")
    text = f"{prefix}{text}{suffix}"
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def evaluate_output(content: str, output_checks: dict[str, Any] | None = None) -> RuleCheckResult:
    checks = output_checks or {}
    text = content or ""
    lowered = text.lower()
    hits: list[str] = []
    blocked_keywords = _as_str_list(checks.get("forbidden_keywords") or checks.get("blocked_keywords"))
    matched = [item for item in blocked_keywords if item and item.lower() in lowered]
    if matched:
        return RuleCheckResult(False, f"命中禁止关键词：{', '.join(matched[:5])}", [f"禁止词:{item}" for item in matched[:5]])
    blocked_regexes = _as_str_list(checks.get("blocked_regex") or checks.get("forbidden_regex"))
    for pattern in blocked_regexes:
        try:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return RuleCheckResult(False, f"命中正则规则：{pattern}", [f"正则:{pattern}"])
        except re.error:
            continue
    if checks.get("forbid_links") or checks.get("no_links"):
        if re.search(r"https?://\S+|t\.me/\S+", text):
            return RuleCheckResult(False, "命中链接规则", ["链接"])
    if checks.get("forbid_mentions") or checks.get("no_mentions"):
        if re.search(r"@\w+", text):
            return RuleCheckResult(False, "命中 @ 提及规则", ["@提及"])
    if checks.get("forbid_contacts") or checks.get("no_contacts"):
        if re.search(r"(\+?\d[\d\-\s]{6,}\d)|([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", text):
            return RuleCheckResult(False, "命中联系方式规则", ["联系方式"])
    min_len = _as_int(checks.get("min_length"))
    if min_len is not None and len(text) < min_len:
        return RuleCheckResult(False, f"输出长度低于最小值 {min_len}", hits)
    max_len = _as_int(checks.get("max_length"))
    if max_len is not None and len(text) > max_len:
        return RuleCheckResult(False, f"输出长度超过最大值 {max_len}", hits)
    return RuleCheckResult(True, "", hits or ["输出校验通过"])


def apply_output_policy(content: str, output_checks: dict[str, Any] | None = None, transforms: dict[str, Any] | None = None) -> OutputPolicyResult:
    checks = output_checks or {}
    first = evaluate_output(content, checks)
    if first.passed:
        return OutputPolicyResult(True, content or "", hits=first.hits)
    strategy = str(checks.get("failure_strategy") or checks.get("on_failure") or "transform_once_drop").strip().lower()
    if strategy in {"transform_once_drop", "transform_once", "转换一次，仍失败则丢弃"}:
        transformed = transform_content(content, transforms or {})
        second = evaluate_output(transformed, checks)
        if second.passed:
            return OutputPolicyResult(True, transformed, action="transform", transformed=transformed != (content or ""), hits=first.hits + second.hits)
        return OutputPolicyResult(False, transformed, reason=second.reason or first.reason, action="drop", transformed=transformed != (content or ""), hits=first.hits + second.hits)
    if strategy in {"fixed_reply", "固定回复"} and checks.get("fixed_reply"):
        fixed = str(checks.get("fixed_reply") or "")
        fixed_check = evaluate_output(fixed, checks)
        return OutputPolicyResult(fixed_check.passed, fixed, reason=fixed_check.reason, action="fixed_reply", transformed=True, hits=first.hits + fixed_check.hits)
    if strategy in {"rewrite_once", "重写", "重新生成一次"}:
        return OutputPolicyResult(False, content or "", reason=first.reason, action="rewrite", hits=first.hits)
    return OutputPolicyResult(False, content or "", reason=first.reason, action="drop", hits=first.hits)


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip().lower() for item in re.split(r"[,，\n]+", value) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    return [str(value).strip().lower()] if str(value).strip() else []


def _as_configured_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，\n]+", value) if item.strip()]
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                items.append(text)
        return items
    text = str(value).strip()
    return [text] if text else []


def _expression_conditions(expression: Any) -> list[dict[str, Any]]:
    if isinstance(expression, dict):
        raw = expression.get("conditions") or expression.get("rules") or []
    elif isinstance(expression, list):
        raw = expression
    else:
        raw = []
    return [item for item in raw if isinstance(item, dict)]


def _passes_expression(content: str, sender_id: str, message_type: str, expression: Any) -> bool:
    conditions = _expression_conditions(expression)
    if not conditions:
        return True
    mode = str(expression.get("mode") or expression.get("logic") or "all").lower() if isinstance(expression, dict) else "all"
    results = [_matches_condition(content, sender_id, message_type, condition) for condition in conditions]
    return any(results) if mode in {"any", "or", "任一"} else all(results)


def _expression_reason(content: str, sender_id: str, message_type: str, expression: Any) -> str:
    conditions = _expression_conditions(expression)
    labels = [_condition_label(condition) for condition in conditions if not _matches_condition(content, sender_id, message_type, condition)]
    mode = str(expression.get("mode") or expression.get("logic") or "all").lower() if isinstance(expression, dict) else "all"
    if mode in {"any", "or", "任一"}:
        return "组合条件未命中任一项：" + "；".join(_condition_label(condition) for condition in conditions[:5])
    return "组合条件未通过：" + "；".join(labels[:5])


def _matches_condition(content: str, sender_id: str, message_type: str, condition: dict[str, Any]) -> bool:
    if condition.get("conditions") or condition.get("rules"):
        return _passes_expression(content, sender_id, message_type, condition)
    field_name = str(condition.get("field") or condition.get("type") or "content").lower()
    operator = str(condition.get("operator") or condition.get("op") or "contains").lower()
    value = condition.get("value")
    if field_name in {"content", "text", "message"}:
        return _match_text(content, operator, value)
    if field_name in {"sender", "sender_id", "user", "user_id"}:
        return _match_text(str(sender_id or ""), operator, value)
    if field_name in {"message_type", "media_type", "type"}:
        return _match_text(str(message_type or "text"), operator, value)
    if field_name in {"length", "message_length", "content_length"}:
        return _match_number(len(content or ""), operator, value)
    return True


def _match_text(left: str, operator: str, value: Any) -> bool:
    left_text = str(left or "").lower()
    values = _as_str_list(value)
    if operator in {"contains", "include", "包含"}:
        return bool(values) and any(item in left_text for item in values)
    if operator in {"not_contains", "exclude", "不包含"}:
        return not any(item in left_text for item in values)
    if operator in {"eq", "equals", "=", "等于"}:
        return bool(values) and left_text in values
    if operator in {"neq", "!=", "not_equals", "不等于"}:
        return left_text not in values
    if operator in {"in", "one_of", "属于"}:
        return bool(values) and left_text in values
    if operator in {"not_in", "不属于"}:
        return left_text not in values
    return True


def _match_number(left: int, operator: str, value: Any) -> bool:
    try:
        right = float(value)
    except (TypeError, ValueError):
        return True
    if operator in {"gte", ">=", "min", "至少"}:
        return left >= right
    if operator in {"lte", "<=", "max", "至多"}:
        return left <= right
    if operator in {"gt", ">", "大于"}:
        return left > right
    if operator in {"lt", "<", "小于"}:
        return left < right
    if operator in {"eq", "=", "等于"}:
        return left == right
    return True


def _condition_label(condition: dict[str, Any]) -> str:
    field_name = condition.get("field") or condition.get("type") or "content"
    operator = condition.get("operator") or condition.get("op") or "contains"
    value = condition.get("value")
    if isinstance(value, list):
        value_text = ",".join(str(item) for item in value[:5])
    else:
        value_text = str(value)
    return f"{field_name} {operator} {value_text}".strip()


__all__ = [
    "OutputPolicyResult",
    "RuleCheckResult",
    "TASK_TYPE_LABELS",
    "apply_output_policy",
    "bound_rule_version",
    "evaluate_input_filter",
    "evaluate_output",
    "task_type_labels",
    "transform_content",
]
