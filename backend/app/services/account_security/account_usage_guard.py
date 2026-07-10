from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import AccountPool, TgAccount, TgAccountSecurityBatchItem
from app.services.account_usage_policy import account_usage, assert_account_action_allowed


SECURITY_MUTATION_ACTION_MAP = {
    "cleanup_devices": "device_cleanup",
    "set_two_fa": "two_fa_set",
    "update_profile": "profile_update",
    "update_username": "profile_update",
    "update_avatar": "profile_update",
}
PROFILE_SECURITY_MUTATION_LABEL = "账号资料/安全变更"
CODE_RECEIVER_RESERVED_REASON = "接码专用账号只允许接收验证码"
CODE_RECEIVER_2FA_CHANGE_DENIED_REASON = "接码专用账号禁止修改二步验证密码"
USAGE_NOT_ALLOWED_REASON = f"账号用途不允许执行{PROFILE_SECURITY_MUTATION_LABEL}"
USAGE_MISMATCH_REASON = f"账号用途不一致，已禁止执行{PROFILE_SECURITY_MUTATION_LABEL}"


@dataclass(frozen=True)
class AccountUsageMutationBlock:
    failure_type: str
    detail: str
    suggested_action: str


def account_security_mutation_block(
    session: Session,
    account: TgAccount,
    action_types: set[str],
) -> AccountUsageMutationBlock | None:
    for action_type in sorted(action_types):
        policy_action = SECURITY_MUTATION_ACTION_MAP.get(action_type)
        if not policy_action:
            continue
        block = _policy_block(session, account, policy_action)
        if block is not None:
            return block
    return None


def assert_account_security_mutation_allowed(
    session: Session,
    account: TgAccount,
    action_type: str,
) -> None:
    block = account_security_mutation_block(session, account, {action_type})
    if block is not None:
        raise ValueError(block.failure_type)


def apply_usage_block_to_batch_item(
    item: TgAccountSecurityBatchItem,
    block: AccountUsageMutationBlock,
    action_types: set[str],
) -> None:
    item.status = "skipped"
    item.precheck_status = "skipped"
    item.skipped_reason = block.detail
    item.failure_type = block.failure_type
    item.failure_detail = block.detail
    for action_type, status_field in _status_fields(action_types):
        setattr(item, status_field, "skipped")


def _policy_block(
    session: Session,
    account: TgAccount,
    policy_action: str,
) -> AccountUsageMutationBlock | None:
    pool = session.get(AccountPool, account.pool_id) if account.pool_id is not None else None
    usage = account_usage(account, pool)
    try:
        assert_account_action_allowed(account, pool, policy_action)
        return None
    except ValueError:
        return _block_for_usage(usage)


def _block_for_usage(usage: str) -> AccountUsageMutationBlock:
    if usage == "code_receiver":
        return AccountUsageMutationBlock(
            failure_type="code_receiver_reserved",
            detail=CODE_RECEIVER_RESERVED_REASON,
            suggested_action="接码专用账号仅保留验证码读取和备用 session 维护能力",
        )
    if usage == "mismatch":
        return AccountUsageMutationBlock(
            failure_type="account_purpose_mismatch",
            detail=USAGE_MISMATCH_REASON,
            suggested_action="修复账号所属分组用途与账号身份投影后再重试",
        )
    return AccountUsageMutationBlock(
        failure_type="account_usage_not_allowed",
        detail=USAGE_NOT_ALLOWED_REASON,
        suggested_action="仅普通账号可执行资料初始化、2FA 或设备清理变更",
    )


def _status_fields(action_types: set[str]) -> list[tuple[str, str]]:
    fields = [
        ("cleanup_devices", "cleanup_status"),
        ("set_two_fa", "two_fa_status"),
        ("update_profile", "profile_status"),
        ("update_username", "username_status"),
        ("update_avatar", "avatar_status"),
    ]
    return [(action_type, field) for action_type, field in fields if action_type in action_types]


__all__ = [
    "CODE_RECEIVER_2FA_CHANGE_DENIED_REASON",
    "CODE_RECEIVER_RESERVED_REASON",
    "account_security_mutation_block",
    "apply_usage_block_to_batch_item",
    "assert_account_security_mutation_allowed",
]
