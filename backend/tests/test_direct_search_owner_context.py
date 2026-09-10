import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.telegram_owner.server import OwnerServer
from app.telegram_owner.request_context import bind_identity, reset_identity, expected_instance

pytestmark = pytest.mark.no_postgres


def test_owner_context_reaches_coroutine_thread_and_is_reset():
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever)
    worker.start()

    async def observe():
        return expected_instance()

    def execute(request, *, caller_connected):
        assert caller_connected()
        return asyncio.run_coroutine_threadsafe(observe(), loop).result(timeout=5)

    server = object.__new__(OwnerServer)
    server.instance_id = "actual-owner"
    server.executor = SimpleNamespace(execute=execute)
    token = bind_identity("caller-root", "caller-context")
    try:
        result = server._execute_owned({"root_identity": "root"}, SimpleNamespace(poll=lambda: False))
        assert result == "actual-owner"
        assert expected_instance() == "caller-context"
    finally:
        reset_identity(token)
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=5)
        assert not worker.is_alive()
        loop.close()
