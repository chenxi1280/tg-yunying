from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Mapping, Sequence, TypeVar

import pytest


SHARD_INDEX_ENV = "PYTEST_SHARD_INDEX"
SHARD_TOTAL_ENV = "PYTEST_SHARD_TOTAL"
_CONFIG_ATTR = "_tgyunying_pytest_shard"
_Item = TypeVar("_Item")


@dataclass(frozen=True)
class ShardConfig:
    index: int
    total: int


def shard_for_nodeid(nodeid: str, shard_total: int) -> int:
    digest = hashlib.sha256(nodeid.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big") % shard_total


def load_shard_config(environ: Mapping[str, str]) -> ShardConfig:
    index = _required_integer(environ, SHARD_INDEX_ENV)
    total = _required_integer(environ, SHARD_TOTAL_ENV)
    if total < 1:
        raise pytest.UsageError(f"{SHARD_TOTAL_ENV} must be at least 1")
    if index < 0 or index >= total:
        raise pytest.UsageError(f"{SHARD_INDEX_ENV} must be between 0 and {total - 1}")
    return ShardConfig(index=index, total=total)


def partition_items(
    items: Sequence[_Item],
    shard: ShardConfig,
) -> tuple[list[_Item], list[_Item]]:
    selected: list[_Item] = []
    deselected: list[_Item] = []
    for item in items:
        belongs_to_shard = shard_for_nodeid(item.nodeid, shard.total) == shard.index
        target = selected if belongs_to_shard else deselected
        target.append(item)
    return selected, deselected


def _required_integer(environ: Mapping[str, str], name: str) -> int:
    raw_value = environ.get(name)
    if raw_value is None:
        raise pytest.UsageError(f"{name} is required when the shard plugin is enabled")
    try:
        return int(raw_value)
    except ValueError as exc:
        raise pytest.UsageError(f"{name} must be an integer, got {raw_value!r}") from exc


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = load_shard_config(os.environ)
    selected, deselected = partition_items(items, shard)
    items[:] = selected
    setattr(config, _CONFIG_ATTR, shard)
    config.hook.pytest_deselected(items=deselected)


def pytest_collection_finish(session: pytest.Session) -> None:
    shard = getattr(session.config, _CONFIG_ATTR)
    selected_count = len(session.items)
    if selected_count == 0:
        raise pytest.UsageError(f"pytest shard {shard.index + 1}/{shard.total} selected no tests")
    print(f"pytest shard {shard.index + 1}/{shard.total}: selected={selected_count}")
