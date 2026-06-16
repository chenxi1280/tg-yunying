from __future__ import annotations

from dataclasses import dataclass

from app.models import AccountStatus


AUTO_RETRY_BUCKET = "auto_retry"
VERIFICATION_BUCKET = "verification"
GROUP_ADMIN_BUCKET = "group_admin"
ACCOUNT_UNAVAILABLE_BUCKET = "account_unavailable"
READY_BUCKET = "ready"
WAITING_BUCKET = "waiting"

_REQUIRED_CHANNEL_MARKERS = ("需要关注", "关注我们的频道", "t.me/", "telegram.me/", "required channel")
_CAPTCHA_MARKERS = ("图形验证码", "验证码", "captcha", "识别图形")
_GROUP_ADMIN_MARKERS = ("管理员解除", "群内由管理员", "被禁言", "ban", "mute", "无发言权限", "群限制")
_UNAVAILABLE_MARKERS = ("frozen account", "frozen accounts", "not available for frozen", "账号不可用", "session", "重新登录")
_TARGET_REF_MARKERS = ("no user has", "could not find the input entity", "cannot find any entity", "目标实体无法解析", "目标群无效", "目标无效")


@dataclass(frozen=True)
class MembershipRecovery:
    bucket: str
    label: str
    action: str
    operator_required: bool = False
    auto_retryable: bool = False
    account_replace_required: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "recovery_bucket": self.bucket,
            "recovery_label": self.label,
            "recovery_action": self.action,
            "operator_required": self.operator_required,
            "auto_retryable": self.auto_retryable,
            "account_replace_required": self.account_replace_required,
        }


def classify_membership_recovery(
    *,
    phase: str,
    account_status: str,
    action_status: str,
    failure_type: str,
    failure_detail: str,
    verification_action: str,
    verification_status: str,
    can_auto_resolve: bool,
) -> MembershipRecovery:
    text = _combined_text(failure_type, failure_detail, verification_action, verification_status)
    if phase == "ready":
        return MembershipRecovery(READY_BUCKET, "已可发言", "无需处理")
    if _account_unavailable(account_status, text):
        return MembershipRecovery(ACCOUNT_UNAVAILABLE_BUCKET, "账号不可用", "剔除该账号并从账号池补位", account_replace_required=True)
    if _is_target_ref_stale(text):
        return MembershipRecovery(AUTO_RETRY_BUCKET, "目标引用刷新", "系统刷新目标群引用后重新入群", auto_retryable=True)
    if _is_required_channel(text):
        return MembershipRecovery(AUTO_RETRY_BUCKET, "自动恢复", "系统自动重新关注前置频道并重查发言权限", auto_retryable=True)
    if verification_action == "识别图形验证码" or _is_captcha(text):
        return MembershipRecovery(VERIFICATION_BUCKET, "验证码处理", "自动识别验证码；失败后进入人工处理并回探测", operator_required=True, auto_retryable=can_auto_resolve)
    if can_auto_resolve:
        return MembershipRecovery(AUTO_RETRY_BUCKET, "自动恢复", "系统自动处理验证并重查发言权限", auto_retryable=True)
    if _is_group_admin_required(text) or phase == "manual_required":
        return MembershipRecovery(GROUP_ADMIN_BUCKET, "群管理员处理", "群管理员解除限制后批量重查", operator_required=True)
    if action_status in {"pending", "claiming", "executing", "retryable_failed"}:
        return MembershipRecovery(WAITING_BUCKET, "系统处理中", "等待准入动作执行")
    return MembershipRecovery(GROUP_ADMIN_BUCKET, "群权限待确认", "确认群限制后批量重查", operator_required=True)


def _combined_text(*parts: str) -> str:
    return " ".join(str(part or "") for part in parts).lower()


def _account_unavailable(account_status: str, text: str) -> bool:
    if account_status and account_status != AccountStatus.ACTIVE.value:
        return True
    return any(marker in text for marker in _UNAVAILABLE_MARKERS)


def _is_required_channel(text: str) -> bool:
    return any(marker.lower() in text for marker in _REQUIRED_CHANNEL_MARKERS)


def _is_captcha(text: str) -> bool:
    return any(marker.lower() in text for marker in _CAPTCHA_MARKERS)


def _is_group_admin_required(text: str) -> bool:
    return any(marker.lower() in text for marker in _GROUP_ADMIN_MARKERS)


def _is_target_ref_stale(text: str) -> bool:
    return any(marker.lower() in text for marker in _TARGET_REF_MARKERS)
