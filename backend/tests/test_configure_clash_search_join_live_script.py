from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "configure_clash_search_join_live.py"
    spec = importlib.util.spec_from_file_location("configure_clash_search_join_live", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.no_postgres
def test_parse_base64_subscription_nodes_and_mihomo_config() -> None:
    script = _load_script()
    lines = "\n".join(
        [
            "trojan://secret@example.com:443?sni=example.com&allowInsecure=1#hk-1",
            "anytls://pass@edge.example.net:8443?sni=edge.example.net&fp=chrome#sg-1",
        ]
    )
    raw = base64.b64encode(lines.encode()).decode()

    nodes = script.parsed_nodes(raw, 8)
    config = script.mihomo_config(nodes[0])

    assert [node.name for node in nodes] == ["hk-1", "sg-1"]
    assert nodes[0].config["type"] == "trojan"
    assert nodes[1].config["type"] == "anytls"
    assert 'mixed-port: 7890' in config
    assert 'MATCH,AUTO' in config


@pytest.mark.no_postgres
def test_parse_clash_yaml_subscription_nodes() -> None:
    script = _load_script()
    raw = """
proxies:
  - name: yaml-trojan
    type: trojan
    server: example.com
    port: 443
    password: secret
  - name: yaml-anytls
    type: anytls
    server: edge.example.net
    port: 8443
    password: pass
"""

    nodes = script.parsed_nodes(raw, 8)

    assert [node.name for node in nodes] == ["yaml-trojan", "yaml-anytls"]
    assert nodes[0].config["type"] == "trojan"


@pytest.mark.no_postgres
def test_protocol_sample_seed_flushes_for_autoflush_disabled_session(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _load_script()

    class FakeSession:
        def __init__(self) -> None:
            self.flushed = False
            self.sample = None

        def scalar(self, _statement):
            return (getattr(self.sample, "id", None) or "seeded") if self.flushed else None

        def add(self, item) -> None:
            if item.__class__.__name__ == "BotProtocolSample":
                self.sample = item

        def flush(self) -> None:
            self.flushed = True

    session = FakeSession()
    monkeypatch.setenv("CLASH_SEED_PROTOCOL_SAMPLE", "true")

    script.require_protocol_sample(session, "jisou")

    assert session.flushed is True
    assert session.sample.bot_username == "jisou"
    assert session.sample.is_active is True
