import asyncio

import pytest

from app.telethon_lifecycle import TelethonClientLifecycle


@pytest.mark.no_postgres
def test_disconnect_waits_for_future_awaitable() -> None:
    completed: list[str] = []

    class FutureReturningClient:
        def disconnect(self):
            async def finish_disconnect() -> None:
                await asyncio.sleep(0)
                completed.append("done")

            return asyncio.shield(asyncio.create_task(finish_disconnect()))

    async def scenario() -> None:
        await TelethonClientLifecycle._disconnect(FutureReturningClient())
        assert completed == ["done"]

    asyncio.run(scenario())
