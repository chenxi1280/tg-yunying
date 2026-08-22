from __future__ import annotations

from .contracts import AuthorizationDrError


MAX_CHUNK_ACCOUNTS = 10


def require_chunk_size(max_accounts: int) -> None:
    if max_accounts < 1 or max_accounts > MAX_CHUNK_ACCOUNTS:
        raise AuthorizationDrError("online_abc_chunk_size_invalid", "Chunk size must be between 1 and 10")


def chunk_result(view: dict, account_ids: list[int], max_accounts: int) -> dict:
    return {
        **view,
        "chunk": {
            "max_accounts": max_accounts,
            "processed_count": len(account_ids),
            "account_ids": list(account_ids),
        },
    }


def require_item_runnable(item) -> None:
    if "blocked" in {item.standby_1_plan, item.standby_2_plan}:
        raise AuthorizationDrError("online_abc_item_blocked", "Frozen account requires repair before login")


def require_slot_ready(plan: str, operation, code: str) -> None:
    if plan == "already_qualified":
        return
    if operation is None or operation.status != "succeeded":
        status = operation.status if operation else "missing"
        raise AuthorizationDrError(code, f"Operation is {status}")


__all__ = [
    "MAX_CHUNK_ACCOUNTS", "chunk_result", "require_chunk_size",
    "require_item_runnable", "require_slot_ready",
]
