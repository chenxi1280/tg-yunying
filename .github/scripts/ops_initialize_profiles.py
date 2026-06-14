from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy import select

from app.database import SessionLocal
from app.models import AccountStatus, Material, Tenant, TgAccount
from app.schemas.account_security import AccountSecurityBatchCreate, AvatarStrategy, ProfileGenerationStrategy
from app.services.account_security import account_security_batch_detail, create_account_security_batch, drain_account_security_batches


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
ACTOR = "codex-prod-profile-init-20260614"


def has_cjk(value: str | None) -> bool:
    return bool(CJK_RE.search(value or ""))


def needs_name_replace(account: TgAccount) -> bool:
    return not has_cjk(account.tg_first_name) or not has_cjk(account.display_name)


def needs_profile_sync(account: TgAccount) -> bool:
    return (account.profile_sync_status or "") != "已同步"


def needs_username(account: TgAccount) -> bool:
    return not (account.username or "").strip()


def needs_avatar(account: TgAccount) -> bool:
    return not (account.avatar_object_key or "").strip()


def needs_initialization(account: TgAccount) -> bool:
    return needs_name_replace(account) or needs_profile_sync(account) or needs_username(account) or needs_avatar(account)


def usable_avatar_materials(session, tenant_id: int) -> list[Material]:
    stmt = (
        select(Material)
        .where(
            Material.tenant_id == tenant_id,
            Material.material_type == "图片",
            Material.review_status == "已审核",
            Material.source_kind == "upload",
            Material.mime_type.in_(["image/jpeg", "image/png", "image/webp"]),
        )
        .order_by(Material.id.asc())
    )
    materials = list(session.scalars(stmt))
    return [material for material in materials if material_usable_for_avatar(material)]


def material_usable_for_avatar(material: Material) -> bool:
    if material.content and Path(material.content).exists():
        return True
    return material.cache_ready_status == "ready" and bool(
        material.tg_cache_account_id and material.tg_cache_peer_id and material.tg_cache_message_id
    )


def account_ids(accounts: list[TgAccount]) -> list[int]:
    return [account.id for account in accounts]


def base_profile_strategy(*, overwrite_existing: bool) -> ProfileGenerationStrategy:
    return ProfileGenerationStrategy(
        generation_mode="ai_random",
        language_style="中文",
        persona_style="自然用户",
        gender_bias="不限",
        age_style="不限",
        bio_enabled=True,
        username_enabled=True,
        username_max_attempts=5,
        overwrite_existing=overwrite_existing,
        custom_prompt="为 Telegram 运营账号生成自然、像真实中文用户的昵称、简介和 username 候选；避免英文名、系统占位名、营销味和重复。",
    )


def create_batch(session, tenant_id: int, ids: list[int], actions: list[str], *, overwrite: bool, avatar_mode: str = "none"):
    if not ids:
        return None
    payload = AccountSecurityBatchCreate(
        account_ids=ids,
        action_types=actions,
        profile_strategy=base_profile_strategy(overwrite_existing=overwrite),
        avatar_strategy=AvatarStrategy(mode=avatar_mode),
        confirm_text="确认",
        reason="线上账号资料初始化：补中文资料、username、头像，修复评论账号未初始化问题",
    )
    batch = create_account_security_batch(session, tenant_id, payload, actor=ACTOR)
    print(
        "CREATED_BATCH",
        json.dumps(
            {
                "tenant_id": tenant_id,
                "batch_id": batch.id,
                "actions": actions,
                "total": batch.total_count,
                "success": batch.success_count,
                "skipped": batch.skipped_count,
                "failed": batch.failed_count,
                "status": batch.status,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return batch.id


def pending_batch_ids(session, batch_ids: list[int]) -> list[int]:
    active = []
    for batch_id in batch_ids:
        detail = account_security_batch_detail(session, 1, batch_id)
        if detail.status in {"running", "ready"}:
            active.append(batch_id)
    return active


def batch_statuses(session, tenant_batches: list[tuple[int, int]]) -> list[dict]:
    rows = []
    for tenant_id, batch_id in tenant_batches:
        detail = account_security_batch_detail(session, tenant_id, batch_id)
        failure_types = Counter(item.failure_type for item in detail.items if item.failure_type)
        item_statuses = Counter(item.status for item in detail.items)
        rows.append(
            {
                "tenant_id": tenant_id,
                "batch_id": batch_id,
                "status": detail.status,
                "total": detail.total_count,
                "success": detail.success_count,
                "skipped": detail.skipped_count,
                "failed": detail.failed_count,
                "item_statuses": dict(item_statuses),
                "failure_types": dict(failure_types),
            }
        )
    return rows


def remaining_reasons(account: TgAccount) -> list[str]:
    reasons = []
    if needs_name_replace(account):
        reasons.append("name_not_chinese")
    if needs_profile_sync(account):
        reasons.append("profile_not_synced")
    if needs_username(account):
        reasons.append("missing_username")
    if needs_avatar(account):
        reasons.append("missing_avatar")
    if account.status != AccountStatus.ACTIVE.value or not account.session_ciphertext:
        reasons.append("offline_or_no_session")
    return reasons


def print_remaining(session) -> int:
    tenants = list(session.scalars(select(Tenant).order_by(Tenant.id.asc())))
    total_remaining = 0
    for tenant in tenants:
        accounts = list(
            session.scalars(
                select(TgAccount)
                .where(TgAccount.tenant_id == tenant.id, TgAccount.deleted_at.is_(None))
                .order_by(TgAccount.id.asc())
            )
        )
        remaining = [account for account in accounts if needs_initialization(account)]
        total_remaining += len(remaining)
        reason_counts = Counter(reason for account in remaining for reason in remaining_reasons(account))
        status_counts = Counter(account.status for account in remaining)
        print(
            "REMAINING",
            json.dumps(
                {
                    "tenant_id": tenant.id,
                    "tenant_name": tenant.name,
                    "count": len(remaining),
                    "status_counts": dict(status_counts),
                    "reason_counts": dict(reason_counts),
                    "account_ids": account_ids(remaining[:50]),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return total_remaining


def main() -> None:
    tenant_batches: list[tuple[int, int]] = []
    blocked_missing_avatar: dict[int, list[int]] = defaultdict(list)
    with SessionLocal() as session:
        tenants = list(session.scalars(select(Tenant).order_by(Tenant.id.asc())))
        for tenant in tenants:
            accounts = list(
                session.scalars(
                    select(TgAccount)
                    .where(TgAccount.tenant_id == tenant.id, TgAccount.deleted_at.is_(None))
                    .order_by(TgAccount.id.asc())
                )
            )
            targets = [account for account in accounts if needs_initialization(account)]
            online_targets = [account for account in targets if account.status == AccountStatus.ACTIVE.value and account.session_ciphertext]
            materials = usable_avatar_materials(session, tenant.id)
            print(
                "TENANT_TARGETS",
                json.dumps(
                    {
                        "tenant_id": tenant.id,
                        "tenant_name": tenant.name,
                        "targets": len(targets),
                        "online_targets": len(online_targets),
                        "usable_avatar_materials": len(materials),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            replace_name_ids = account_ids([account for account in online_targets if needs_name_replace(account)])
            sync_profile_ids = account_ids(
                [account for account in online_targets if account.id not in replace_name_ids and needs_profile_sync(account)]
            )
            username_ids = account_ids([account for account in online_targets if needs_username(account)])
            avatar_accounts = [account for account in online_targets if needs_avatar(account)]
            if avatar_accounts and not materials:
                blocked_missing_avatar[tenant.id].extend(account_ids(avatar_accounts))
            avatar_ids = account_ids(avatar_accounts if materials else [])
            for batch_id in [
                create_batch(session, tenant.id, replace_name_ids, ["update_profile"], overwrite=True),
                create_batch(session, tenant.id, sync_profile_ids, ["update_profile"], overwrite=False),
                create_batch(session, tenant.id, username_ids, ["update_username"], overwrite=False),
                create_batch(session, tenant.id, avatar_ids, ["update_avatar"], overwrite=False, avatar_mode="random_from_material_pool"),
            ]:
                if batch_id is not None:
                    tenant_batches.append((tenant.id, batch_id))
        for tenant_id, ids in blocked_missing_avatar.items():
            print(
                "BLOCKED_MISSING_AVATAR_MATERIALS",
                json.dumps({"tenant_id": tenant_id, "count": len(ids), "account_ids": ids[:50]}, ensure_ascii=False),
                flush=True,
            )

    while True:
        processed = drain_account_security_batches(SessionLocal, limit=20)
        with SessionLocal() as session:
            statuses = batch_statuses(session, tenant_batches)
        print("BATCH_STATUSES", json.dumps(statuses, ensure_ascii=False), flush=True)
        active = [row for row in statuses if row["status"] in {"running", "ready"}]
        if not active:
            break
        if processed == 0:
            time.sleep(30)

    with SessionLocal() as session:
        remaining_total = print_remaining(session)
    print("FINAL_REMAINING_TOTAL", remaining_total, flush=True)


if __name__ == "__main__":
    main()
