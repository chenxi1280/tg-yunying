from __future__ import annotations

import argparse
import asyncio

from app.database import SessionLocal
from app.integrations.telegram.gateway import TelethonTelegramGateway
from app.models import TgAccount
from app.security import decrypt_session
from app.services.developer_apps import credentials_for_account
from app.services.task_center.ai_group_prompt import sanitize_group_message_text


DEFAULT_MESSAGE = "这家水汇环境怎么样，有老哥去过吗？"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="精确目标 Telegram 纯文本真实发送验证")
    parser.add_argument("--apply", action="store_true", help="明确执行真实 Telegram 发送与撤回")
    parser.add_argument("--account-id", type=int, help="精确测试账号 ID")
    parser.add_argument("--target", action="append", default=[], help="精确目标，可重复传入")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="待验证纯文本")
    return parser.parse_args(argv)


def validate_apply_args(args: argparse.Namespace) -> None:
    if not args.apply:
        return
    if not args.account_id or args.account_id <= 0:
        raise ValueError("--apply 必须同时提供有效 --account-id")
    if not args.target:
        raise ValueError("--apply 必须至少提供一个精确 --target")
    if any(not str(target).strip() for target in args.target):
        raise ValueError("--target 不能为空")


def run(args: argparse.Namespace) -> int:
    validate_apply_args(args)
    cleaned = sanitize_group_message_text(args.message)
    if not args.apply:
        print("PREVIEW_ONLY: 未连接数据库或 Telegram，未发送任何消息。")
        print(f"cleaned_message={cleaned!r}")
        print(f"targets={list(args.target)} account_id={args.account_id}")
        return 0
    if not cleaned:
        raise ValueError("清洗后消息为空，拒绝执行")
    return asyncio.run(_run_apply(args, cleaned))


async def _run_apply(args: argparse.Namespace, cleaned: str) -> int:
    with SessionLocal() as session:
        account = session.get(TgAccount, args.account_id)
        if account is None:
            raise ValueError(f"账号不存在: {args.account_id}")
        credentials = credentials_for_account(session, account)
        gateway = TelethonTelegramGateway()
        raw_session = decrypt_session(account.session_ciphertext)
        client = await gateway._get_or_create_client(credentials, raw_session)
        failures = []
        for target in args.target:
            failure = await _exercise_target(
                gateway,
                client,
                account=account,
                credentials=credentials,
                target=str(target).strip(),
                cleaned=cleaned,
            )
            if failure:
                failures.append(failure)
    return _report_results(failures)


async def _exercise_target(
    gateway: TelethonTelegramGateway,
    client,
    *,
    account: TgAccount,
    credentials,
    target: str,
    cleaned: str,
) -> str:
    result = await gateway._send_async(
        account.session_ciphertext,
        target,
        cleaned,
        None,
        credentials,
    )
    if not result.ok or not result.remote_message_id:
        return f"target={target} send_failed detail={result.detail or result.failure_type}"
    try:
        entity = await client.get_entity(target)
        await client.delete_messages(entity, [int(result.remote_message_id)])
    except Exception as exc:
        return f"target={target} cleanup_failed message_id={result.remote_message_id} detail={exc}"
    print(f"target={target} send_and_cleanup_ok message_id={result.remote_message_id}")
    return ""


def _report_results(failures: list[str]) -> int:
    if not failures:
        print("LIVE_TEST_PASSED: 所有精确目标均已发送并成功撤回。")
        return 0
    print("LIVE_TEST_FAILED")
    for failure in failures:
        print(failure)
    return 1


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
