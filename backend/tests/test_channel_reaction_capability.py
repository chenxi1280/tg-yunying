import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from telethon.tl import types

from app.integrations.telegram.telethon_content import fetch_channel_reaction_capability


pytestmark = pytest.mark.no_postgres
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.anyio
async def test_fetch_channel_reaction_capability_resolves_all_active_standard_emojis() -> None:
    client = AsyncMock()
    client.get_entity.return_value = SimpleNamespace(id=1)
    client.side_effect = [
        SimpleNamespace(full_chat=SimpleNamespace(available_reactions=types.ChatReactionsAll())),
        SimpleNamespace(
            reactions=[
                SimpleNamespace(reaction="👍", inactive=False),
                SimpleNamespace(reaction="🔥", inactive=False),
                SimpleNamespace(reaction="👎", inactive=True),
                SimpleNamespace(reaction="🤩", inactive=False, premium=True),
            ]
        ),
    ]

    capability = await fetch_channel_reaction_capability(client, "-1001")

    assert capability.mode == "all"
    assert capability.available_reactions == ("👍", "🔥")


@pytest.mark.anyio
async def test_fetch_channel_reaction_capability_keeps_only_standard_some_reactions() -> None:
    client = AsyncMock()
    client.get_entity.return_value = SimpleNamespace(id=1)
    client.side_effect = [
        SimpleNamespace(
            full_chat=SimpleNamespace(
                available_reactions=types.ChatReactionsSome(
                    reactions=[
                        types.ReactionEmoji(emoticon="👏"),
                        types.ReactionEmoji(emoticon="🤩"),
                        types.ReactionCustomEmoji(document_id=42),
                    ]
                )
            )
        ),
        SimpleNamespace(
            reactions=[
                SimpleNamespace(reaction="👏", inactive=False, premium=False),
                SimpleNamespace(reaction="🤩", inactive=False, premium=True),
            ]
        ),
    ]

    capability = await fetch_channel_reaction_capability(client, "-1001")

    assert capability.mode == "some"
    assert capability.available_reactions == ("👏",)


@pytest.mark.anyio
async def test_fetch_channel_reaction_capability_distinguishes_none_from_unknown() -> None:
    client = AsyncMock()
    client.get_entity.return_value = SimpleNamespace(id=1)
    client.side_effect = [
        SimpleNamespace(full_chat=SimpleNamespace(available_reactions=types.ChatReactionsNone())),
        SimpleNamespace(full_chat=SimpleNamespace(available_reactions=object())),
    ]

    none_capability = await fetch_channel_reaction_capability(client, "-1001")
    unknown_capability = await fetch_channel_reaction_capability(client, "-1001")

    assert none_capability.mode == "none"
    assert unknown_capability.mode == "unknown"


def test_reaction_capability_migration_adds_and_removes_columns() -> None:
    engine = sa.create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE operation_targets (id INTEGER PRIMARY KEY)"))
        migration = _load_reaction_capability_migration()
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()
        columns = {item["name"] for item in sa.inspect(connection).get_columns("operation_targets")}
        assert {"reaction_capability_mode", "available_reactions"} <= columns

        migration.downgrade()
        columns = {item["name"] for item in sa.inspect(connection).get_columns("operation_targets")}
        assert "reaction_capability_mode" not in columns
        assert "available_reactions" not in columns


def _load_reaction_capability_migration():
    path = PROJECT_ROOT / "backend/migrations/versions/0170_channel_reaction_capability.py"
    spec = importlib.util.spec_from_file_location("migration_0170_channel_reaction_capability", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.anyio
async def test_successful_full_channel_without_reactions_is_none() -> None:
    client = AsyncMock()
    client.get_entity.return_value = SimpleNamespace(id=1)
    client.return_value = SimpleNamespace(full_chat=SimpleNamespace(available_reactions=None))
    capability = await fetch_channel_reaction_capability(client, "-1001")
    assert capability.mode == "none"
    assert capability.available_reactions == ()
    assert client.await_count == 1


@pytest.mark.anyio
@pytest.mark.parametrize("response", [SimpleNamespace(), SimpleNamespace(full_chat=None),
                                      SimpleNamespace(full_chat=SimpleNamespace())])
async def test_malformed_full_channel_is_not_a_disabled_capability(response) -> None:
    client = AsyncMock()
    client.get_entity.return_value = SimpleNamespace(id=1)
    client.return_value = response
    with pytest.raises(RuntimeError, match="channel_reaction_capability_response_invalid"):
        await fetch_channel_reaction_capability(client, "-1001")


@pytest.mark.anyio
async def test_reaction_capability_network_error_remains_an_error() -> None:
    client = AsyncMock()
    client.get_entity.return_value = SimpleNamespace(id=1)
    client.side_effect = TimeoutError("capability probe timed out")
    with pytest.raises(TimeoutError):
        await fetch_channel_reaction_capability(client, "-1001")


def test_disabled_capability_is_visible_in_task_failure_reason() -> None:
    from app.services.task_center.executors.channel_like_capability import message_reaction_plan

    channel = SimpleNamespace(reaction_capability_mode="none", available_reactions=[])
    session = SimpleNamespace(get=lambda *_args: channel)
    task = SimpleNamespace(stats={}, last_error="")
    message = SimpleNamespace(id=1, channel_target_id=1, current_source_revision_id=None, content_preview="")
    result = message_reaction_plan(session, task, message, config={}, reactions=["👍"], quantity=30, seed_id="source")
    assert result == []
    assert task.stats["reaction_capability_unavailable"]["capability_mode"] == "none"
    assert "频道未开放点赞表情" in task.last_error
