"""Diagnose Tianjin rescue admin account and admission status."""

from __future__ import annotations
from sqlalchemy import text, select
from app.database import SessionLocal
from app.models import Tenant, TgAccount, TgGroup, TgGroupAccount, Action, Task

def main():
    session = SessionLocal()
    try:
        tenant = session.get(Tenant, 1)
        print("=== TENANT GROUP RESCUE CONFIG ===")
        print(f"group_rescue_enabled: {getattr(tenant, 'group_rescue_enabled', None)}")
        print(f"group_rescue_admin_account_id: {getattr(tenant, 'group_rescue_admin_account_id', None)}")

        admin_id = getattr(tenant, "group_rescue_admin_account_id", None)
        if admin_id:
            admin_acc = session.get(TgAccount, admin_id)
            if admin_acc:
                print(f"Rescue Admin Account: id={admin_acc.id}, phone={admin_acc.phone}, username={admin_acc.username}, status={admin_acc.status}, has_session={bool(admin_acc.session_ciphertext)}")
            else:
                print("Rescue Admin Account NOT FOUND in tg_accounts!")
        else:
            print("NO rescue admin account configured on tenant!")

        # Check candidate settingbother or 管理 accounts
        candidates = list(session.scalars(
            select(TgAccount).where(
                TgAccount.tenant_id == 1,
                TgAccount.deleted_at.is_(None),
                TgAccount.username.in_(["settingbother", "tianjin_admin"])
            )
        ).all())
        print(f"\nCandidate Admin Accounts Found: {len(candidates)}")
        for c in candidates:
            print(f"  - id={c.id}, username={c.username}, phone={c.phone}, status={c.status}, has_session={bool(c.session_ciphertext)}")

        # Check Tianjin Music Group (5999) and Tianjin Yipinlou (5828)
        for gid in [5999, 5828]:
            grp = session.get(TgGroup, gid)
            if not grp:
                print(f"\nGroup {gid} NOT FOUND!")
                continue
            print(f"\n=== GROUP {gid} ({grp.title}) ===")
            print(f"tg_peer_id: {grp.tg_peer_id}, auth: {grp.auth_status}, member_count: {grp.member_count}, can_send: {grp.can_send}")
            
            # Group accounts count
            cnt = session.scalar(
                select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == gid)
            )
            can_send_cnt = session.scalar(
                select(text("count(*)")).select_from(TgGroupAccount).where(TgGroupAccount.group_id == gid, TgGroupAccount.can_send.is_(True))
            )
            print(f"GroupAccounts in DB: total={cnt}, can_send={can_send_cnt}")

            # If rescue admin is in group_accounts
            if admin_id:
                link = session.scalar(
                    select(TgGroupAccount).where(TgGroupAccount.group_id == gid, TgGroupAccount.account_id == admin_id)
                )
                print(f"Rescue Admin in Group link: {link is not None}")

            # Recent ensure_target_membership actions
            actions = list(session.scalars(
                select(Action).where(
                    Action.payload["group_id"].as_integer() == gid,
                    Action.action_type == "ensure_target_membership",
                ).order_by(Action.created_at.desc()).limit(5)
            ).all())
            print(f"Recent ensure_target_membership actions: {len(actions)}")
            for a in actions:
                res = a.result or {}
                print(f"  - action={a.id}, status={a.status}, rescue_status={res.get('rescue_status')}, rescue_detail={res.get('rescue_detail')}, err={res.get('error_code')}, time={a.created_at}")

    finally:
        session.close()

if __name__ == "__main__":
    main()
