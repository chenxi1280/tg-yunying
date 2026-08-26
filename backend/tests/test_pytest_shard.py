from dataclasses import dataclass

import pytest

from scripts.pytest_shard import ShardConfig, load_shard_config, partition_items, shard_for_nodeid


pytestmark = pytest.mark.no_postgres


@dataclass(frozen=True)
class FakeItem:
    nodeid: str


def test_shard_mapping_is_deterministic_complete_and_disjoint() -> None:
    items = [FakeItem(nodeid=f"tests/test_example.py::test_case[{index}]") for index in range(200)]
    selected_by_shard = [partition_items(items, ShardConfig(index=index, total=5))[0] for index in range(5)]
    selected_nodeids = [item.nodeid for shard_items in selected_by_shard for item in shard_items]

    assert sorted(selected_nodeids) == sorted(item.nodeid for item in items)
    assert len(selected_nodeids) == len(set(selected_nodeids))
    assert all(shard_for_nodeid(item.nodeid, 5) == index for index, values in enumerate(selected_by_shard) for item in values)


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({}, "PYTEST_SHARD_INDEX is required"),
        ({"PYTEST_SHARD_INDEX": "x", "PYTEST_SHARD_TOTAL": "2"}, "must be an integer"),
        ({"PYTEST_SHARD_INDEX": "0", "PYTEST_SHARD_TOTAL": "0"}, "must be at least 1"),
        ({"PYTEST_SHARD_INDEX": "2", "PYTEST_SHARD_TOTAL": "2"}, "must be between 0 and 1"),
    ],
)
def test_invalid_shard_configuration_fails(environ: dict[str, str], message: str) -> None:
    with pytest.raises(pytest.UsageError, match=message):
        load_shard_config(environ)


def test_valid_shard_configuration_is_immutable() -> None:
    shard = load_shard_config({"PYTEST_SHARD_INDEX": "1", "PYTEST_SHARD_TOTAL": "3"})

    assert shard == ShardConfig(index=1, total=3)
