"""Owner probes borrow the existing main connection instead of opening a second one."""
from app.integrations.telegram.contracts import AccountHealth


async def health(gateway, raw_session, credentials, *, connect_timeout_seconds=None):
    from app.integrations.telegram.account_freeze import read_account_freeze_health

    client = await gateway._get_or_create_client(
        credentials, raw_session, connect_timeout_seconds=connect_timeout_seconds,
    )
    if not await client.is_user_authorized():
        return AccountHealth(status="需重新登录", health_score=45, detail="session 已失效")
    await client.get_me()
    return await read_account_freeze_health(client)
