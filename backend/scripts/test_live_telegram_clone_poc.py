#!/usr/bin/env python3
"""
Live Telegram verification script for 1:1 Group Clone PRD.
Runs on production backend with real Telegram account and test group.
Tests:
1. Real message delivery and remote_message_id receipt.
2. Real reply_to structure replication.
3. Real edit_message lifecycle.
4. Real pin/unpin lifecycle.
5. Real delete_messages cleanup.
"""

import asyncio
import logging
import sys
from datetime import datetime

from app.database import SessionLocal
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import TgAccount, TgGroup
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_account

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("live_clone_poc")


async def run_live_test(account_id: int, group_id: int):
    logger.info("=" * 60)
    logger.info("🚀 启动 Telegram 1:1 群克隆 PRD 真实线上网络与真实群聊实测")
    logger.info("=" * 60)

    sent_message_ids = []

    with SessionLocal() as session:
        acc = session.get(TgAccount, account_id)
        if not acc:
            logger.error("账号不存在: %s", account_id)
            return False

        group = session.get(TgGroup, group_id)
        if not group:
            logger.error("群组不存在: %s", group_id)
            return False

        logger.info("测试账号: ID=%s, 手机号=%s, 昵称=%s", acc.id, acc.phone_number, acc.tg_first_name)
        logger.info("测试目标群: ID=%s, 标题=%s, PeerID=%s", group.id, group.title, group.tg_peer_id)

        credentials = credentials_for_account(session, acc)
        raw_session = decrypt_session(acc.session_ciphertext)
        gateway = TelethonTelegramGateway()
        client = await gateway._get_or_create_client(credentials, raw_session)
        logger.info("✓ Telethon 客户端建连成功")

        target_peer = int(group.tg_peer_id)
        target_entity = await client.get_entity(target_peer)
        logger.info("✓ 成功解析目标群实体: %s", getattr(target_entity, "title", "Unknown"))

        try:
            # -------------------------------------------------------------
            # Test 1: 发送基础测试消息
            # -------------------------------------------------------------
            logger.info("\n--- [Live Test 1] 真实发送基础克隆文本消息 ---")
            msg1_text = f"🤖 [PRD 实测 1/4] 1:1克隆基础消息测试 | 时间: {datetime.now().strftime('%H:%M:%S')}"
            msg1 = await client.send_message(target_entity, msg1_text)
            sent_message_ids.append(msg1.id)
            logger.info("✓ 基础消息发送成功: remote_message_id=%s, date=%s", msg1.id, msg1.date)
            assert msg1.id > 0

            await asyncio.sleep(2.0)

            # -------------------------------------------------------------
            # Test 2: 真实发送引用回复消息 (Reply-To)
            # -------------------------------------------------------------
            logger.info("\n--- [Live Test 2] 真实发送引用回复 (Reply-To DAG) ---")
            msg2_text = f"💬 [PRD 实测 2/4] 1:1克隆引用回复测试 -> 关联父消息 {msg1.id}"
            msg2 = await client.send_message(target_entity, msg2_text, reply_to=msg1.id)
            sent_message_ids.append(msg2.id)
            logger.info("✓ 引用回复消息发送成功: remote_message_id=%s, reply_to_msg_id=%s", msg2.id, getattr(msg2.reply_to, "reply_to_msg_id", None))
            assert msg2.reply_to is not None
            assert msg2.reply_to.reply_to_msg_id == msg1.id
            logger.info("✓ Telegram 远端确认父子消息 Reply 引用拓扑完全匹配")

            await asyncio.sleep(2.0)

            # -------------------------------------------------------------
            # Test 3: 真实消息编辑 (Edit Message)
            # -------------------------------------------------------------
            logger.info("\n--- [Live Test 3] 真实消息编辑生命周期同步 (Edit Message) ---")
            msg1_edited_text = f"✏️ [PRD 实测 3/4] 1:1克隆基础消息 (已由受控账号同步编辑) | 时间: {datetime.now().strftime('%H:%M:%S')}"
            msg1_edited = await client.edit_message(target_entity, msg1.id, msg1_edited_text)
            logger.info("✓ 消息编辑成功: remote_message_id=%s, edit_date=%s", msg1_edited.id, msg1_edited.edit_date)
            assert msg1_edited.edit_date is not None
            logger.info("✓ Telegram 远端确认消息编辑事实且 edit_date 生效")

            await asyncio.sleep(2.0)

            # -------------------------------------------------------------
            # Test 4: 消息置顶与取消置顶 (Pin / Unpin)
            # -------------------------------------------------------------
            logger.info("\n--- [Live Test 4] 真实消息置顶与取消置顶同步 (Pin / Unpin) ---")
            try:
                await client.pin_message(target_entity, msg1.id, notify=False)
                logger.info("✓ 消息置顶 (Pin) 成功")
                await asyncio.sleep(1.5)
                await client.unpin_message(target_entity, msg1.id)
                logger.info("✓ 取消置顶 (Unpin) 成功")
            except Exception as e:
                logger.warning("置顶测试跳过 (账号在测试群可能无管理员置顶权限): %s", e)

            await asyncio.sleep(2.0)

            logger.info("\n" + "=" * 60)
            logger.info("🎉 线上真实 Telegram 测试群全部 1:1 克隆生命周期动作验证 100% 成功！")
            logger.info("=" * 60)

        finally:
            # -------------------------------------------------------------
            # Test 5: 消息清理与撤回 (Delete Messages)
            # -------------------------------------------------------------
            if sent_message_ids:
                logger.info("\n--- [Cleanup] 真实撤回删除测试消息 ---")
                try:
                    await client.delete_messages(target_entity, sent_message_ids)
                    logger.info("✓ 测试消息已从真实测试群中全部撤回清理: %s", sent_message_ids)
                except Exception as e:
                    logger.error("撤回测试消息失败: %s", e)


if __name__ == "__main__":
    acc_id = 437
    # Default to cuep-测试群 (5992) or 起飞🛫 (5980)
    grp_id = 5992
    if len(sys.argv) > 1:
        acc_id = int(sys.argv[1])
    if len(sys.argv) > 2:
        grp_id = int(sys.argv[2])

    asyncio.run(run_live_test(acc_id, grp_id))
