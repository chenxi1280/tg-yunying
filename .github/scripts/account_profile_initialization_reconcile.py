from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, AiAccountVoiceProfile, Material, TgAccount
from app.services.account_profile_auto_init import profile_is_ready, queue_profile_initialization_for_accounts
from app.services.task_center.account_voice_profiles import ensure_voice_profiles_for_accounts, generate_voice_profiles_with_ai


ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
TENANT_ID = int(os.getenv("ACCOUNT_PROFILE_RECONCILE_TENANT_ID", "1"))
APPLY = os.getenv("ACCOUNT_PROFILE_RECONCILE_APPLY", "false").lower() in {"1", "true", "yes"}
DRAIN_ONCE = os.getenv("ACCOUNT_PROFILE_RECONCILE_DRAIN_ONCE", "false").lower() in {"1", "true", "yes"}
MAX_SAMPLE = 50
VOICE_PROFILE_COMMIT_CHUNK_SIZE = int(os.getenv("ACCOUNT_PROFILE_RECONCILE_VOICE_COMMIT_CHUNK_SIZE", "2"))


def main() -> int:
    before = _inspect_accounts()
    result = None
    processed = 0
    voice_profile_result = _empty_voice_profile_result()
    if APPLY and _should_apply_reconcile(before):
        profile_account_ids = _not_ready_account_ids(before)
        voice_profile_account_ids = _missing_voice_profile_account_ids_from_payload(before)
        if voice_profile_account_ids:
            voice_profile_result = _reconcile_voice_profiles(voice_profile_account_ids)
        with SessionLocal() as session:
            if profile_account_ids:
                result = queue_profile_initialization_for_accounts(
                    session,
                    tenant_id=TENANT_ID,
                    account_ids=profile_account_ids,
                    actor="github-actions",
                    reason="生产账号资料初始化补齐：全量检查后自动创建",
                )
            session.commit()
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
        "created_voice_profiles": voice_profile_result["created"],
        "voice_profile_reconcile": voice_profile_result,
        "queued_account_ids": list(result.queued_account_ids) if result else [],
        "batch_ids": list(result.batch_ids) if result else [],
    }
    print("ACCOUNT_PROFILE_RECONCILE=" + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
    _assert_reconcile_effective(before, after, APPLY, voice_profile_result.get("error"))
    return 0


def _reconcile_voice_profiles(account_ids: list[int]) -> dict[str, object]:
    result = _empty_voice_profile_result()
    for batch in _chunks(account_ids, VOICE_PROFILE_COMMIT_CHUNK_SIZE):
        try:
            created = _commit_voice_profile_batch(batch)
        except Exception as exc:  # noqa: BLE001 - print structured progress before failing the gate.
            result["failed_batch_account_ids"] = batch
            result["error"] = _error_summary(exc)
            return result
        result["created"] = int(result["created"]) + int(created)
        result["completed_account_ids"].extend(batch)
        _print_voice_profile_progress(result)
    return result


def _commit_voice_profile_batch(account_ids: list[int]) -> int:
    with SessionLocal() as session:
        generator = generate_voice_profiles_with_ai(session, tenant_id=TENANT_ID)
        created = ensure_voice_profiles_for_accounts(
            session,
            tenant_id=TENANT_ID,
            account_ids=account_ids,
            generator=generator,
        )
        session.commit()
        return created


def _empty_voice_profile_result() -> dict[str, Any]:
    return {
        "created": 0,
        "completed_account_ids": [],
        "failed_batch_account_ids": [],
        "commit_chunk_size": VOICE_PROFILE_COMMIT_CHUNK_SIZE,
        "error": None,
    }


def _print_voice_profile_progress(result: dict[str, object]) -> None:
    print("ACCOUNT_PROFILE_RECONCILE_PROGRESS=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


def _chunks(values: list[int], size: int) -> list[list[int]]:
    if size <= 0:
        raise ValueError("ACCOUNT_PROFILE_RECONCILE_VOICE_COMMIT_CHUNK_SIZE must be positive")
    return [values[index:index + size] for index in range(0, len(values), size)]


def _error_summary(exc: Exception) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


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
    raw_ids = _not_ready_account_ids(before) + _missing_voice_profile_account_ids_from_payload(before)
    return list(dict.fromkeys(int(account_id) for account_id in raw_ids))


def _not_ready_account_ids(before: dict[str, object]) -> list[int]:
    return [int(account_id) for account_id in list(before.get("not_ready_account_ids") or [])]


def _missing_voice_profile_account_ids_from_payload(before: dict[str, object]) -> list[int]:
    return [int(account_id) for account_id in list(before.get("missing_voice_profile_account_ids") or [])]


def _assert_reconcile_effective(
    before: dict[str, object],
    after: dict[str, object],
    apply: bool,
    voice_error: object | None = None,
) -> None:
    if not apply:
        return
    if voice_error:
        raise RuntimeError(f"voice profile reconcile failed: {voice_error}")
    before_missing = int(before.get("missing_voice_profile_count") or 0)
    after_missing = int(after.get("missing_voice_profile_count") or 0)
    if before_missing and after_missing:
        raise RuntimeError(f"voice profile reconcile did not complete: before={before_missing}, after={after_missing}")


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
