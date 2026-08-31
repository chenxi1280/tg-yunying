#!/usr/bin/env python3
"""
Test script: Scrape real Telegram group members (excluding our AI accounts and teachers/spammers),
extract natural display names and bios, and simulate copying/assigning them to platform accounts.

Usage:
    python -m scripts.test_copy_group_profiles [--mode telethon|database|hybrid] [--sample-limit 100] [--target-accounts 20]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

# Add backend directory to sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.database import SessionLocal
from app.models import (
    GroupContextMessage,
    TgAccount,
    TgGroup,
)
from app.services.account_profile_name_generation import generate_username_variants
from scripts.profile_candidate_filter import (
    GroupCandidateProfile,
    ProfileFilter,
    load_system_exclusions,
)
from scripts.profile_live_candidates import scrape_live_group_participants

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("test_copy_group_profiles")


def extract_candidates_from_messages(
    session: Session,
    profile_filter: ProfileFilter,
    sample_limit: int = 500,
) -> list[GroupCandidateProfile]:
    rows = _message_sender_rows(session, sample_limit)
    candidates: list[GroupCandidateProfile] = []
    seen_names: set[str] = set()
    rejections: Counter[str] = Counter()
    for row in rows:
        candidate, rejection = _message_candidate(
            row,
            profile_filter,
            seen_names=seen_names,
        )
        if candidate is None:
            rejections[rejection] += 1
            continue
        candidates.append(candidate)
    logger.info(
        "Message filtering summary: %d accepted, rejections=%s",
        len(candidates),
        dict(rejections.most_common(5)),
    )
    return candidates


def _message_sender_rows(session: Session, sample_limit: int) -> list:
    return session.execute(
        select(
            GroupContextMessage.sender_peer_id,
            GroupContextMessage.sender_name,
            GroupContextMessage.sender_username,
            TgGroup.title,
            TgGroup.tg_peer_id,
        )
        .join(TgGroup, TgGroup.id == GroupContextMessage.group_id)
        .where(
            GroupContextMessage.is_bot.is_(False),
            GroupContextMessage.sender_name != "",
            GroupContextMessage.sender_name != "真人用户",
        )
        .distinct(GroupContextMessage.sender_peer_id)
        .limit(sample_limit)
    ).all()


def _message_candidate(
    row,
    profile_filter: ProfileFilter,
    *,
    seen_names: set[str],
) -> tuple[GroupCandidateProfile | None, str]:
    peer_id, name, username, group_title, group_peer_id = row
    display_name = name.strip()
    username = (username or "").strip()
    result = profile_filter.filter_candidate(
        user_id=str(peer_id),
        display_name=display_name,
        username=username,
    )
    if not result.is_valid:
        return None, result.rejection_reason
    normalized = ProfileFilter.normalize_name(display_name)
    if normalized in seen_names:
        return None, "duplicate_name"
    seen_names.add(normalized)
    parts = display_name.split(" ", 1)
    return GroupCandidateProfile(
        source_type="context_message",
        group_title=group_title or "Unknown Group",
        group_peer_id=str(group_peer_id or ""),
        user_id=str(peer_id),
        username=username,
        display_name=display_name,
        first_name=parts[0],
        last_name=parts[1] if len(parts) > 1 else "",
        bio="",
        collected_at=datetime.utcnow().isoformat(),
    ), ""


def adjust_display_name(raw_name: str, seed: int = 0) -> tuple[str, str, str]:
    """
    Adjust a copied real display name with natural variations (e.g. bracket cleanup, subtle styles).
    Returns (display_name, first_name, last_name).
    """
    clean_name = raw_name.strip()

    # Strip unnecessary brackets/annotations if too long (e.g. '许校长（哈利波特）' -> '许校长' or '哈利波特')
    m_bracket = re.match(r"^([^\(（]+)[\(（]([^\)）]+)[\)）]$", clean_name)
    if m_bracket:
        part1 = m_bracket.group(1).strip()
        part2 = m_bracket.group(2).strip()
        choices = [part1, part2, f"{part1}_{part2}", clean_name]
        chosen = choices[seed % len(choices)]
        clean_name = chosen if chosen else part1

    # Split into first and last name if space exists
    parts = clean_name.split(" ", 1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""

    return clean_name, first_name, last_name


def simulate_profile_allocation(
    session: Session,
    candidate_profiles: list[GroupCandidateProfile],
    num_accounts: int = 15,
) -> dict[str, Any]:
    accounts = session.scalars(
        select(TgAccount)
        .where(TgAccount.status == "在线")
        .order_by(TgAccount.id.asc())
        .limit(num_accounts)
    ).all()
    if not accounts:
        accounts = session.scalars(
            select(TgAccount).order_by(TgAccount.id.asc()).limit(num_accounts)
        ).all()
    allocations = [
        _profile_allocation(account, candidate_profiles[index % len(candidate_profiles)], index)
        for index, account in enumerate(accounts)
    ]
    return {"total_allocated": len(allocations), "allocations": allocations}


def _profile_allocation(
    account: TgAccount,
    candidate: GroupCandidateProfile,
    index: int,
) -> dict[str, Any]:
    display_name, first_name, last_name = adjust_display_name(
        candidate.display_name,
        seed=index + account.id,
    )
    usernames = generate_username_variants(
        raw_username=candidate.username,
        display_name=display_name,
        seed=index + account.id,
    )
    return {
        "account_id": account.id,
        "phone_masked": account.phone_masked,
        "before": {
            "display_name": account.display_name or "",
            "username": account.username or "(未设置)",
            "tg_first_name": account.tg_first_name or "",
            "tg_last_name": account.tg_last_name or "",
            "tg_bio": account.tg_bio or "",
        },
        "after": {
            "display_name": display_name,
            "tg_first_name": first_name,
            "tg_last_name": last_name,
            "username_candidates": usernames,
            "tg_bio": candidate.bio.strip(),
            "source_group": candidate.group_title,
            "copied_from_username": candidate.username or "(无)",
            "copied_from_user_id": candidate.user_id,
        },
    }


# ----------------------------------------------------------------------
# Main Runner & CLI
# ----------------------------------------------------------------------

async def run_pipeline(
    mode: str,
    sample_limit: int,
    target_accounts: int,
    *,
    account_id: int | None = None,
) -> dict[str, Any]:
    with SessionLocal() as session:
        our_ids, our_usernames, our_names, teachers = load_system_exclusions(session)
        profile_filter = ProfileFilter(
            our_ids,
            our_usernames,
            our_names,
            task_discussion_teachers=teachers,
        )

        live_candidates: list[GroupCandidateProfile] = []
        msg_candidates: list[GroupCandidateProfile] = []

        if mode in ("telethon", "hybrid"):
            if account_id is None:
                raise ValueError("live_account_id_required")
            logger.info("=== Running Live Telethon Group Scraping ===")
            live_candidates = await scrape_live_group_participants(
                session,
                profile_filter,
                account_id=account_id,
                max_groups=4,
                per_group_limit=sample_limit // 2,
            )

        if mode in ("database", "hybrid") or not live_candidates:
            logger.info("=== Running Database Group Context Messages Extraction ===")
            msg_candidates = extract_candidates_from_messages(
                session, profile_filter, sample_limit=sample_limit,
            )

        all_candidates = live_candidates + msg_candidates
        logger.info("Total qualified real profiles collected: %d", len(all_candidates))

        if not all_candidates:
            logger.error("No valid candidate profiles collected.")
            return {"status": "error", "message": "no_candidates_found"}

        logger.info("=== Simulating Profile Allocation to Platform Accounts ===")
        allocation_result = simulate_profile_allocation(
            session, all_candidates, num_accounts=target_accounts,
        )

        return {
            "status": "success",
            "mode": mode,
            "counts": {
                "live_candidates": len(live_candidates),
                "msg_candidates": len(msg_candidates),
                "total_candidates": len(all_candidates),
                "accounts_tested": allocation_result["total_allocated"],
            },
            "sample_candidates": [asdict(c) for c in all_candidates[:20]],
            "allocations": allocation_result["allocations"],
        }


def print_report(results: dict[str, Any]) -> None:
    print("\n" + "=" * 85)
    print(" 🚀 REAL GROUP MEMBER PROFILE & USERNAME MUTATION TEST REPORT")
    print("=" * 85)
    print(f"Status: {results.get('status')}")
    print(f"Mode: {results.get('mode')}")
    counts = results.get("counts", {})
    print(f"Total Qualified Real User Profiles Found: {counts.get('total_candidates', 0)}")
    print(f"  - Live Telethon Group Participants: {counts.get('live_candidates', 0)}")
    print(f"  - Group Message Send History:       {counts.get('msg_candidates', 0)}")
    print(f"Simulated Allocation Targets:         {counts.get('accounts_tested', 0)}")

    print("\n" + "-" * 85)
    print(" 📋 SAMPLE QUALIFIED REAL PROFILES COPIED FROM OTHER GROUPS")
    print("-" * 85)
    for i, c in enumerate(results.get("sample_candidates", [])[:12], 1):
        u_str = f"@{c['username']}" if c.get("username") else "(无用户名)"
        bio_str = f" | Bio: {c['bio']!r}" if c.get("bio") else " | Bio: (留空)"
        print(f" {i:2d}. Name: {c['display_name']!r:<20} | User: {u_str:<18} (Group: {c['group_title']}){bio_str}")

    print("\n" + "-" * 85)
    print(" 🔄 BEFORE vs AFTER ACCOUNT PROFILE ALLOCATION COMPARISON (DRY RUN)")
    print("-" * 85)
    for item in results.get("allocations", []):
        acc_id = item["account_id"]
        phone = item["phone_masked"]
        b_name = item["before"]["display_name"]
        b_user = item["before"]["username"]
        b_bio = item["before"]["tg_bio"]

        a_name = item["after"]["display_name"]
        a_user_cands = item["after"]["username_candidates"]
        a_bio = item["after"]["tg_bio"] if item["after"]["tg_bio"] else "(留空)"
        src = item["after"]["source_group"]
        src_u = item["after"]["copied_from_username"]

        print(f"\n[Account #{acc_id} | {phone}]")
        print(f"  ❌ BEFORE:")
        print(f"     • Name:     {b_name!r}")
        print(f"     • Username: {b_user!r}")
        print(f"     • Bio:      {b_bio!r}")
        print(f"  ✅ AFTER (Copied & Adjusted from {src} | 原用户名: {src_u}):")
        print(f"     • Name:     {a_name!r}")
        print(f"     • Username Candidates: {a_user_cands}")
        print(f"     • Bio:      {a_bio!r}")

    print("\n" + "=" * 85 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Test copying real group profiles for platform accounts")
    parser.add_argument("--mode", choices=["telethon", "database", "hybrid"], default="database", help="Extraction mode")
    parser.add_argument("--account-id", type=int, default=None, help="Exact account used by live modes")
    parser.add_argument("--sample-limit", type=int, default=100, help="Max group participants to scan")
    parser.add_argument("--target-accounts", type=int, default=15, help="Number of accounts to simulate allocation for")
    args = parser.parse_args()
    if args.mode in ("telethon", "hybrid") and args.account_id is None:
        parser.error("--account-id is required for telethon/hybrid mode")

    pipeline_kwargs = {"account_id": args.account_id} if args.account_id is not None else {}
    results = asyncio.run(
        run_pipeline(
            args.mode,
            args.sample_limit,
            args.target_accounts,
            **pipeline_kwargs,
        )
    )
    print_report(results)


if __name__ == "__main__":
    main()
