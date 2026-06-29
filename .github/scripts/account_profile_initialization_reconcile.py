from __future__ import annotations

import json
import os
import re
from collections import Counter

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, AiAccountVoiceProfile, Material, TgAccount
from app.services.account_profile_auto_init import profile_is_ready, queue_profile_initialization_for_accounts


ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
TENANT_ID = int(os.getenv("ACCOUNT_PROFILE_RECONCILE_TENANT_ID", "1"))
APPLY = os.getenv("ACCOUNT_PROFILE_RECONCILE_APPLY", "false").lower() in {"1", "true", "yes"}
DRAIN_ONCE = os.getenv("ACCOUNT_PROFILE_RECONCILE_DRAIN_ONCE", "false").lower() in {"1", "true", "yes"}
MAX_SAMPLE = 50


def main() -> int:
    before = _inspect_accounts()
    result = None
    processed = 0
    if APPLY and _should_apply_reconcile(before):
        with SessionLocal() as session:
            result = queue_profile_initialization_for_accounts(
                session,
                tenant_id=TENANT_ID,
                account_ids=_reconcile_account_ids(before),
                actor="github-actions",
                reason="生产账号资料初始化补齐：全量检查后自动创建",
            )
        if DRAIN_ONCE:
            from app.worker import drain_once

            processed = drain_once(200, role="account-security")
    after = _inspect_accounts()
    payload = {
        "tenant_id": TENANT_ID,
        "apply": APPLY,
        "drain_once": DRAIN_ONCE,
        "worker_processed": processed,
        "before": before,
        "after": after,
        "queued_account_ids": list(result.queued_account_ids) if result else [],
        "batch_ids": list(result.batch_ids) if result else [],
    }
    print("ACCOUNT_PROFILE_RECONCILE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    return 0


def _inspect_accounts() -> dict[str, object]:
    with SessionLocal() as session:
        accounts = _active_accounts(session)
        missing_voice_ids = _missing_voice_profile_account_ids(session, accounts)
        samples = [_account_summary(account) for account in accounts if not profile_is_ready(account)]
        return {
            "active_account_count": len(accounts),
            "ready_count": len(accounts) - len(samples),
            "not_ready_count": len(samples),
            "not_ready_account_ids": [item["account_id"] for item in samples],
            "missing_voice_profile_count": len(missing_voice_ids),
            "missing_voice_profile_account_ids": missing_voice_ids,
            "not_ready_reason_counts": dict(Counter(reason for item in samples for reason in item["reasons"])),
            "not_ready_samples": samples[:MAX_SAMPLE],
            "avatar_materials": _avatar_material_summary(session),
        }


def _should_apply_reconcile(before: dict[str, object]) -> bool:
    return bool(int(before.get("not_ready_count") or 0) or int(before.get("missing_voice_profile_count") or 0))


def _reconcile_account_ids(before: dict[str, object]) -> list[int]:
    raw_ids = list(before.get("not_ready_account_ids") or []) + list(before.get("missing_voice_profile_account_ids") or [])
    return list(dict.fromkeys(int(account_id) for account_id in raw_ids))


def _missing_voice_profile_account_ids(session, accounts: list[TgAccount]) -> list[int]:
    account_ids = [account.id for account in accounts]
    if not account_ids:
        return []
    ready_ids = set(
        session.scalars(
            select(AiAccountVoiceProfile.account_id).where(
                AiAccountVoiceProfile.tenant_id == TENANT_ID,
                AiAccountVoiceProfile.account_id.in_(account_ids),
                AiAccountVoiceProfile.status == "active",
                AiAccountVoiceProfile.quality_status == "active",
            )
        )
    )
    return [account_id for account_id in account_ids if account_id not in ready_ids]


def _active_accounts(session) -> list[TgAccount]:
    return list(
        session.scalars(
            select(TgAccount)
            .where(
                TgAccount.tenant_id == TENANT_ID,
                TgAccount.deleted_at.is_(None),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.session_ciphertext.is_not(None),
                TgAccount.session_ciphertext != "",
            )
            .order_by(TgAccount.id.asc())
        )
    )


def _account_summary(account: TgAccount) -> dict[str, object]:
    return {
        "account_id": account.id,
        "display_name": account.display_name,
        "tg_first_name": account.tg_first_name,
        "tg_last_name": account.tg_last_name,
        "username": account.username,
        "has_avatar": bool(account.avatar_object_key),
        "profile_sync_status": account.profile_sync_status,
        "reasons": _not_ready_reasons(account),
    }


def _not_ready_reasons(account: TgAccount) -> list[str]:
    reasons: list[str] = []
    if not _is_chinese_name(account.display_name):
        reasons.append("display_name_not_chinese")
    if not _is_chinese_name(account.tg_first_name):
        reasons.append("tg_first_name_not_chinese")
    if _has_ascii(account.tg_last_name):
        reasons.append("tg_last_name_has_english")
    if not str(account.username or "").strip():
        reasons.append("missing_username")
    if not str(account.avatar_object_key or "").strip():
        reasons.append("missing_avatar")
    return reasons


def _avatar_material_summary(session) -> dict[str, int]:
    materials = list(
        session.scalars(
            select(Material).where(
                Material.tenant_id == TENANT_ID,
                Material.material_type == "图片",
                Material.review_status == "已审核",
                Material.source_kind == "upload",
            )
        )
    )
    return {
        "reviewed_uploaded_images": len(materials),
        "cache_ready": sum(1 for material in materials if material.cache_ready_status == "ready"),
        "cache_failed": sum(1 for material in materials if material.cache_ready_status == "cache_failed"),
    }


def _is_chinese_name(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(text and re.search(r"[\u4e00-\u9fff]", text) and not _has_ascii(text))


def _has_ascii(value: str | None) -> bool:
    return bool(ASCII_LETTER_RE.search(str(value or "")))


if __name__ == "__main__":
    raise SystemExit(main())
