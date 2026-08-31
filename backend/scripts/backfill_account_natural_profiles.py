"""
Script to safely backfill platform accounts with realistic, non-duplicate profiles
extracted from non-AI, non-teacher real Telegram group members.

Features:
  1. Strict Preview -> Manifest -> Apply contract (100% fidelity via overrides).
  2. Production safety gates (normal operational pool, no open batch, mutation block check).
  3. Action types restricted to PRD scope: update_profile + update_avatar.
  4. Full tenant isolation and strictly < 50 lines per function.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# Add backend directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import SessionLocal
from app.models import (
    AccountPool,
    AccountStatus,
    TgAccount,
    TgAccountSecurityBatch,
    TgAccountSecurityBatchItem,
)
from app.schemas.account_security import (
    AccountSecurityBatchCreate,
    AccountSecurityProfileOverride,
    AvatarStrategy,
    ProfileGenerationStrategy,
)
from app.services.account_profile_identity import unavailable_name_keys
from app.services.account_profile_login_batch_init import (
    allocate_avatar_sources,
    ready_avatar_materials,
)
from app.services.account_security.service import (
    account_security_mutation_block,
    create_account_security_batch,
)
from scripts.profile_candidate_filter import (
    GENERIC_NAMES,
    OLD_SYNTHETIC_STEMS,
    NaturalCandidateProfile,
    ProfileFilter,
    extract_group_profiles,
    load_system_exclusions,
    unique_display_name_from_candidate,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_natural_profiles")

MANIFEST_VERSION = "2.0"
MAX_BATCH_SIZE = 50


def is_legacy_profile(account: TgAccount) -> bool:
    name = (account.display_name or "").strip()
    if not name or name in GENERIC_NAMES or name.startswith("账号_"):
        return True
    return any(stem in name for stem in OLD_SYNTHETIC_STEMS)


def load_eligible_target_accounts(
    session: Session,
    *,
    limit: int | None = None,
    ratio: float = 0.7,
    tenant_id: int | None = None,
    pool_id: int | None = None,
    pool_name: str | None = None,
) -> list[TgAccount]:
    query = _target_account_query(tenant_id, pool_id, pool_name)
    candidates = session.scalars(query.order_by(TgAccount.id.asc())).all()
    open_batch_accounts = _load_open_batch_account_ids(session)
    specific_pool = pool_id is not None or bool(pool_name)
    eligible: list[TgAccount] = []
    for acc in candidates:
        if acc.id in open_batch_accounts:
            continue
        if not specific_pool and not is_legacy_profile(acc):
            continue
        if account_security_mutation_block(session, acc, {"update_profile", "update_avatar"}) is not None:
            continue
        eligible.append(acc)

    target_count = _target_count(
        limit,
        ratio,
        candidates=candidates,
        eligible=eligible,
        specific_pool=specific_pool,
    )
    if len(eligible) < target_count:
        _fill_eligible_accounts(
            session,
            candidates,
            eligible=eligible,
            open_batch_accounts=open_batch_accounts,
            target_count=target_count,
        )
    return eligible[:target_count] if target_count > 0 else eligible


def _target_account_query(
    tenant_id: int | None,
    pool_id: int | None,
    pool_name: str | None,
):
    query = (
        select(TgAccount)
        .join(AccountPool, TgAccount.pool_id == AccountPool.id)
        .where(
            TgAccount.status == AccountStatus.ACTIVE.value,
            TgAccount.deleted_at.is_(None),
            TgAccount.session_ciphertext.is_not(None),
            TgAccount.account_identity == "normal",
            AccountPool.pool_purpose == "normal",
        )
    )
    if tenant_id is not None:
        query = query.where(TgAccount.tenant_id == tenant_id)
    if pool_id is not None:
        query = query.where(TgAccount.pool_id == pool_id)
    if pool_name:
        query = query.where(AccountPool.name == pool_name)
    return query


def _target_count(
    limit: int | None,
    ratio: float,
    *,
    candidates: list[TgAccount],
    eligible: list[TgAccount],
    specific_pool: bool,
) -> int:
    if limit is not None:
        return limit
    if specific_pool:
        return len(eligible)
    return max(1, int(len(candidates) * ratio)) if candidates else 0


def _fill_eligible_accounts(
    session: Session,
    candidates: list[TgAccount],
    *,
    eligible: list[TgAccount],
    open_batch_accounts: set[int],
    target_count: int,
) -> None:
    for acc in candidates:
        if acc.id in open_batch_accounts or acc in eligible:
            continue
        if account_security_mutation_block(session, acc, {"update_profile", "update_avatar"}) is not None:
            continue
        eligible.append(acc)
        if len(eligible) >= target_count:
            return


def _load_open_batch_account_ids(session: Session) -> set[int]:
    return set(
        session.scalars(
            select(TgAccountSecurityBatchItem.account_id)
            .join(TgAccountSecurityBatch, TgAccountSecurityBatchItem.batch_id == TgAccountSecurityBatch.id)
            .where(TgAccountSecurityBatch.status.in_(["ready", "running", "waiting"]))
        ).all()
    )


def generate_plan(
    target_accounts: list[TgAccount],
    candidates: list[NaturalCandidateProfile],
    *,
    avatar_sources: list[str],
    unavailable_keys: set[str] | None = None,
    forbidden_words: set[str] | None = None,
) -> list[dict[str, Any]]:
    if target_accounts and not candidates:
        raise ValueError("profile_candidate_source_empty")
    if len(avatar_sources) != len(target_accounts):
        raise ValueError("avatar_source_count_mismatch")
    used_name_keys = set(unavailable_keys or set())
    plan: list[dict[str, Any]] = []

    for idx, acc in enumerate(target_accounts):
        cand = candidates[idx % len(candidates)]
        disp_name, f_name, l_name = unique_display_name_from_candidate(
            cand.display_name,
            used_name_keys,
            seed_idx=idx + acc.id,
            forbidden_words=forbidden_words,
        )
        plan.append({
            "account_id": acc.id,
            "tenant_id": acc.tenant_id or 1,
            "phone_masked": acc.phone_masked,
            "current_display_name": acc.display_name or "",
            "current_username": acc.username or "",
            "current_bio": acc.tg_bio or "",
            "proposed_display_name": disp_name,
            "proposed_first_name": f_name,
            "proposed_last_name": l_name,
            "proposed_bio": acc.tg_bio or "",
            "proposed_avatar_source": avatar_sources[idx],
            "source_group": cand.group_title,
        })
    return plan


def _plan_hash(plan: list[dict[str, Any]]) -> str:
    serialized = json.dumps(plan, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_manifest(plan: list[dict[str, Any]], tenant_id: int | None = None) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "plan_hash": _plan_hash(plan),
        "item_count": len(plan),
        "items": plan,
    }


def run_preview(
    *,
    limit: int | None = None,
    ratio: float = 0.7,
    tenant_id: int | None = None,
    pool_id: int | None = None,
    pool_name: str | None = None,
    output_manifest: str | None = None,
) -> dict[str, Any]:
    if tenant_id is None:
        raise ValueError("tenant_id_required")
    with SessionLocal() as session:
        our_ids, our_usernames, our_names, teachers = load_system_exclusions(session, tenant_id=tenant_id)
        profile_filter = ProfileFilter(
            our_ids,
            our_usernames,
            our_names,
            task_discussion_teachers=teachers,
        )

        target_accounts = load_eligible_target_accounts(
            session,
            limit=limit,
            ratio=ratio,
            tenant_id=tenant_id,
            pool_id=pool_id,
            pool_name=pool_name,
        )
        if not target_accounts:
            logger.info("No eligible accounts found.")
            return {}

        plan = _build_preview_plan(
            session,
            target_accounts,
            profile_filter,
            tenant_id=tenant_id,
            scope_key=str(pool_id or pool_name or "default"),
        )
        manifest = build_manifest(plan, tenant_id=tenant_id)

        _print_preview_summary(plan, manifest)
        if output_manifest:
            with open(output_manifest, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2, ensure_ascii=False)
            logger.info("Saved plan manifest to %s", output_manifest)
        return manifest


def _build_preview_plan(
    session: Session,
    target_accounts: list[TgAccount],
    profile_filter: ProfileFilter,
    *,
    tenant_id: int,
    scope_key: str,
) -> list[dict[str, Any]]:
    candidates = extract_group_profiles(
        session,
        profile_filter,
        limit=max(len(target_accounts) * 3, 50),
        tenant_id=tenant_id,
    )
    avatar_sources = _preview_avatar_sources(
        session,
        tenant_id=tenant_id,
        target_count=len(target_accounts),
        scope_key=scope_key,
    )
    return generate_plan(
        target_accounts,
        candidates,
        avatar_sources=avatar_sources,
        unavailable_keys=unavailable_name_keys(session, tenant_id),
    )


def _preview_avatar_sources(
    session: Session,
    *,
    tenant_id: int,
    target_count: int,
    scope_key: str,
) -> list[str]:
    materials = ready_avatar_materials(session, tenant_id)
    seed = f"profile-backfill:{tenant_id}:{scope_key}"
    return allocate_avatar_sources(materials, target_count, seed)


def _print_preview_summary(plan: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    print("\n" + "=" * 90)
    print(f" 🚀 PROFILE UPDATE RUNNER (PREVIEW) - {len(plan)} ACCOUNTS | Plan SHA: {manifest['plan_hash'][:12]}")
    print("=" * 90)
    for item in plan:
        print(f"\n[Account #{item['account_id']} | {item['phone_masked']}]")
        print(f"  ❌ Current:  Name={item['current_display_name']!r}, Username={item['current_username']!r}, Bio={item['current_bio']!r}")
        print(f"  ✅ Proposed: Name={item['proposed_display_name']!r}")
        print(f"              Avatar={item['proposed_avatar_source']} <from: {item['source_group']}>")
    print("\n" + "=" * 90)
    print(f"Summary: {len(plan)} accounts planned with frozen names and avatar sources.")
    print("=" * 90 + "\n")


def _validated_manifest_items(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("manifest_version_invalid")
    tenant_id = manifest.get("tenant_id")
    if not isinstance(tenant_id, int) or tenant_id <= 0:
        raise ValueError("manifest_tenant_invalid")
    items = manifest.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("manifest_items_invalid")
    if manifest.get("item_count") != len(items):
        raise ValueError("manifest_item_count_mismatch")
    required = {
        "account_id", "tenant_id", "proposed_display_name", "proposed_first_name",
        "proposed_last_name", "proposed_bio", "proposed_avatar_source",
    }
    if any(not required.issubset(item) for item in items):
        raise ValueError("manifest_item_contract_invalid")
    if any(item.get("tenant_id") != tenant_id for item in items):
        raise ValueError("manifest_tenant_mismatch")
    actual_hash = _plan_hash(items)
    if not hmac.compare_digest(str(manifest.get("plan_hash") or ""), actual_hash):
        raise ValueError("manifest_hash_mismatch")
    return items


def run_apply(manifest_path: str) -> None:
    if not os.path.exists(manifest_path):
        logger.error("Manifest file not found: %s", manifest_path)
        sys.exit(1)

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    items = _validated_manifest_items(manifest)
    logger.info("Loaded %d items from manifest (Plan SHA: %s)", len(items), manifest.get("plan_hash", "")[:12])

    with SessionLocal() as session:
        by_tenant: dict[int, list[dict[str, Any]]] = {}
        for item in items:
            t_id = item.get("tenant_id") or 1
            by_tenant.setdefault(t_id, []).append(item)

        for t_id, tenant_items in by_tenant.items():
            for offset in range(0, len(tenant_items), MAX_BATCH_SIZE):
                _execute_tenant_batch(
                    session,
                    t_id,
                    tenant_items[offset:offset + MAX_BATCH_SIZE],
                )


def _execute_tenant_batch(session: Session, tenant_id: int, items: list[dict[str, Any]]) -> None:
    acc_ids = [it["account_id"] for it in items]
    overrides = [
        AccountSecurityProfileOverride(
            account_id=it["account_id"],
            generated_display_name=it["proposed_display_name"],
            generated_first_name=it["proposed_first_name"],
            generated_last_name=it["proposed_last_name"],
            generated_bio=it["proposed_bio"],
            username_candidates=[],
            avatar_source=it["proposed_avatar_source"],
        )
        for it in items
    ]
    batch_payload = AccountSecurityBatchCreate(
        account_ids=acc_ids,
        action_types=["update_profile", "update_avatar"],
        confirm_text="确认",
        reason="统一更新为真实群成员自然资料（去重化）",
        profile_strategy=ProfileGenerationStrategy(
            generation_mode="template",
            bio_enabled=True,
            username_enabled=False,
            overwrite_existing=True,
        ),
        avatar_strategy=AvatarStrategy(mode="none"),
        preview_overrides=overrides,
    )
    batch = create_account_security_batch(session, tenant_id, batch_payload, actor="script:backfill_natural_profiles")
    logger.info("Tenant %d: Security Batch #%d created with status %s", tenant_id, batch.id, batch.status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely backfill natural profiles for accounts")
    parser.add_argument("--mode", choices=["preview", "apply"], required=True, help="Mode: preview or apply")
    parser.add_argument("--limit", type=int, default=None, help="Explicit max number of accounts to process")
    parser.add_argument("--ratio", type=float, default=0.7, help="Ratio of accounts to process (default 0.7 = 70%)")
    parser.add_argument("--tenant-id", type=int, default=None, help="Optional tenant ID filter")
    parser.add_argument("--pool-id", type=int, default=None, help="Target specific account pool by ID")
    parser.add_argument("--pool-name", type=str, default=None, help="Target specific account pool by name")
    parser.add_argument("--output-manifest", type=str, default=None, help="Path to save preview manifest JSON")
    parser.add_argument("--manifest-file", type=str, default=None, help="Path to manifest JSON for apply mode")
    args = parser.parse_args()

    if args.mode == "preview" and not args.tenant_id:
        logger.error("--tenant-id is required for tenant-isolated preview.")
        sys.exit(1)

    if args.mode == "preview":
        manifest_out = args.output_manifest or f"manifest_profile_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
        run_preview(
            limit=args.limit,
            ratio=args.ratio,
            tenant_id=args.tenant_id,
            pool_id=args.pool_id,
            pool_name=args.pool_name,
            output_manifest=manifest_out,
        )
    elif args.mode == "apply":
        if not args.manifest_file:
            logger.error("--manifest-file is required in apply mode")
            sys.exit(1)
        run_apply(manifest_path=args.manifest_file)


if __name__ == "__main__":
    main()
