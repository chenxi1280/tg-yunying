import asyncio
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Action, Tenant, TgAccount
from app.security import decrypt_session
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.services.developer_apps import credentials_for_account

async def check_admin():
    with SessionLocal() as session:
        tenant = session.get(Tenant, 1)
        admin_acc = session.get(TgAccount, tenant.group_rescue_admin_account_id) if tenant and tenant.group_rescue_admin_account_id else None
        if not admin_acc:
            print("❌ 错误: 未找到管理员账号")
            return

        print(f"【救援管理员账号】 ID: {admin_acc.id} | 昵称: {admin_acc.display_name} | 手机: {admin_acc.phone_masked}")

        # 1. Inspect the 2 pending rescue actions
        acts = list(session.scalars(select(Action).where(Action.action_type == "invite_group_account").order_by(Action.created_at.desc())))
        print(f"\n数据库中所有 invite_group_account Action (共 {len(acts)} 条):")
        for a in acts:
            print(f"  Action ID: {a.id} | Status: {a.status} | ScheduledAt: {a.scheduled_at} | ExecutedAt: {a.executed_at}")
            print(f"    ClaimOwner: {a.claim_owner} | LeaseOwner: {a.lease_owner} | Result: {a.result}")
            print(f"    Payload: {a.payload}")

        # 2. Check admin account in @yuebao8
        raw_session = decrypt_session(admin_acc.session_ciphertext)
        credentials = credentials_for_account(session, admin_acc)
        gateway = TelethonTelegramGateway()
        client = await gateway._get_or_create_client(credentials, raw_session)
        me = await client.get_me()
        print(f"\n✅ 管理员客户端登录: @{getattr(me, 'username', '无')} (TG ID: {me.id})")

        try:
            from telethon import functions, types
            entity = await client.get_entity("@yuebao8")
            print(f"实体解析成功: {entity.title} (ID: {entity.id})")

            # Check admin's participant status
            part = await client(functions.channels.GetParticipantRequest(channel=entity, participant=me))
            print(f"管理员在群内的身份: {type(part.participant).__name__}")

            is_admin = isinstance(part.participant, (types.ChannelParticipantAdmin, types.ChannelParticipantCreator))
            print(f"是否为管理员/群主: {is_admin}")
            if is_admin:
                admin_rights = getattr(part.participant, "admin_rights", None)
                print(f"管理员权限 (admin_rights): {admin_rights}")
                if admin_rights:
                    print(f"  - 是否能邀请成员 (invite_users): {getattr(admin_rights, 'invite_users', False)}")
                    print(f"  - 是否能封禁/解封 (ban_users): {getattr(admin_rights, 'ban_users', False)}")
                    print(f"  - 是否能管理群 (manage_call): {getattr(admin_rights, 'manage_call', False)}")
            else:
                print("⚠️ 警告: 该账号在 @yuebao8 群内只是【普通成员】，并不是管理员！无法审批或强制邀请新成员入群！")

        except Exception as e:
            print(f"探测异常: {type(e).__name__}: {e}")

if __name__ == "__main__":
    asyncio.run(check_admin())
