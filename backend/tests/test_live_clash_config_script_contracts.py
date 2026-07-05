from __future__ import annotations

from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / ".github/scripts/configure_clash_search_join_live.py"

pytestmark = pytest.mark.no_postgres


def test_live_clash_script_writes_airport_subscription_nodes_and_slot_bindings() -> None:
    source = SCRIPT.read_text()

    assert "ProxyAirportSubscription" in source
    assert "ProxyAirportNode" in source
    assert "ensure_or_create_search_join_environment" in source
    assert "proxy_airport_node_id=" in source
    assert "subscription_id=" in source
    assert "node_key=" in source
    assert "healthy_node_count" in source
    assert "def ensure_scoped_airport_binding" in source
    assert "environment.proxy_binding_id = scoped_binding.id" in source
    assert "environment.proxy_id = scoped_binding.proxy_id" in source
