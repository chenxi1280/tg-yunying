from __future__ import annotations
import base64
import json
import math
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import yaml
from sqlalchemy import func, select
from app.database import SessionLocal
from app.models import (
    AccountProxy,
    AccountProxyBinding,
    AccountStatus,
    Action,
    BotProtocolSample,
    OperationTarget,
    ProxyHealthCheck,
    Task,
    TgAccount,
    TgAccountAuthorization,
)
from app.schemas.task_center import AccountConfig, SearchJoinBotConfig, SearchJoinGroupTaskCreate
from app.services._common import _now, audit
from app.services.task_center.executors import build_task_plan
from app.services.task_center.service import _assert_precheck_allows_start, _mark_task_started, _new_task

MIXED_PORT = 7890
CONFIG_DIR = Path(os.getenv("CLASH_CONFIG_DIR", "/tmp/tgyunying-mihomo-configs"))
CONTAINER_PREFIX = os.getenv("CLASH_CONTAINER_PREFIX", "tgyunying-mihomo")
DEFAULT_TARGET_QUERY = "郑州"
DEFAULT_SEARCH_BOT = "jisou"
MAX_SUBSCRIPTION_BYTES = 5 * 1024 * 1024
NODE_SKIP_RE = re.compile(r"(剩余|套餐|到期|官网|流量|expire|traffic)", re.IGNORECASE)

@dataclass(frozen=True)
class ProxyNode:
    index: int
    name: str
    config: dict[str, Any]
    @property
    def container_name(self) -> str:
        return f"{CONTAINER_PREFIX}-{self.index:03d}"
def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
def env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default

def subscription_url() -> str:
    url = os.getenv("CLASH_SUBSCRIPTION_URL", "").strip()
    if not url:
        raise RuntimeError("CLASH_SUBSCRIPTION_URL is required")
    return url

def fetch_subscription(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "tg-yunying-production-clash-setup/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read(MAX_SUBSCRIPTION_BYTES + 1)
    if len(raw) > MAX_SUBSCRIPTION_BYTES:
        raise RuntimeError("subscription response exceeds 5MiB safety limit")
    return raw.decode("utf-8", errors="replace")

def decode_base64_text(raw: str) -> str:
    compact = "".join(raw.strip().split())
    padded = compact + "=" * (-len(compact) % 4)
    return base64.b64decode(padded, validate=False).decode("utf-8", errors="replace")

def subscription_text(raw: str) -> str:
    if "://" in raw or "proxies:" in raw:
        return raw
    decoded = decode_base64_text(raw)
    return decoded if decoded.strip() else raw

def parsed_nodes(raw: str, limit: int) -> list[ProxyNode]:
    text = subscription_text(raw)
    configs = yaml_proxy_configs(text) if "proxies:" in text else uri_proxy_configs(text)
    nodes = [ProxyNode(index=index, name=str(config["name"]), config=config) for index, config in enumerate(configs[:limit], start=1)]
    if not nodes:
        raise RuntimeError("no supported proxy nodes parsed")
    return nodes

def uri_proxy_configs(text: str) -> list[dict[str, Any]]:
    configs: list[dict[str, Any]] = []
    skipped = 0
    for line in [item.strip() for item in text.splitlines()]:
        if not line or "://" not in line or NODE_SKIP_RE.search(line):
            continue
        config = parse_proxy_uri(line, len(configs) + 1)
        if not config:
            skipped += 1
            continue
        configs.append(config)
    if not configs:
        raise RuntimeError(f"no supported proxy nodes parsed, skipped={skipped}")
    return configs

def yaml_proxy_configs(text: str) -> list[dict[str, Any]]:
    loaded = yaml.safe_load(text)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("proxies"), list):
        raise RuntimeError("clash yaml subscription missing proxies list")
    configs: list[dict[str, Any]] = []
    for item in loaded["proxies"]:
        if not isinstance(item, dict) or not item.get("name") or not item.get("type"):
            continue
        node = dict(item)
        node["name"] = sanitize_name(node["name"])
        configs.append(node)
    if not configs:
        raise RuntimeError("clash yaml subscription has no usable proxy nodes")
    return configs

def parse_proxy_uri(uri: str, index: int) -> dict[str, Any] | None:
    scheme = uri.split(":", 1)[0].lower()
    parsers = {
        "trojan": parse_trojan,
        "anytls": parse_anytls,
        "ss": parse_shadowsocks,
        "vmess": parse_vmess,
        "vless": parse_vless,
    }
    parser = parsers.get(scheme)
    return parser(uri, index) if parser else None

def parse_trojan(uri: str, index: int) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qs(parsed.query)
    node = base_node("trojan", parsed, index)
    node["password"] = urllib.parse.unquote(parsed.username or "")
    tls_options(node, query)
    return node

def parse_anytls(uri: str, index: int) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qs(parsed.query)
    node = base_node("anytls", parsed, index)
    node["password"] = urllib.parse.unquote(parsed.username or "")
    tls_options(node, query)
    return node

def parse_shadowsocks(uri: str, index: int) -> dict[str, Any] | None:
    parsed = urllib.parse.urlsplit(uri)
    userinfo = urllib.parse.unquote(parsed.username or "")
    if ":" not in userinfo:
        userinfo = decode_base64_text(userinfo)
    if ":" not in userinfo:
        return None
    cipher, password = userinfo.split(":", 1)
    node = base_node("ss", parsed, index)
    node["cipher"] = cipher
    node["password"] = password
    return node

def parse_vmess(uri: str, index: int) -> dict[str, Any] | None:
    payload = decode_base64_text(uri.split("://", 1)[1])
    data = json.loads(payload)
    server = str(data.get("add") or "").strip()
    port = int(data.get("port") or 0)
    uuid = str(data.get("id") or "").strip()
    if not server or not port or not uuid:
        return None
    node = {"name": sanitize_name(data.get("ps") or f"vmess-{index:03d}"), "type": "vmess"}
    node.update({"server": server, "port": port, "uuid": uuid, "alterId": int(data.get("aid") or 0), "cipher": "auto"})
    if str(data.get("tls") or "").strip():
        node["tls"] = True
        node["servername"] = str(data.get("sni") or data.get("host") or "")
    apply_network(node, str(data.get("net") or ""), str(data.get("host") or ""), str(data.get("path") or ""))
    return node

def parse_vless(uri: str, index: int) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(uri)
    query = urllib.parse.parse_qs(parsed.query)
    node = base_node("vless", parsed, index)
    node["uuid"] = urllib.parse.unquote(parsed.username or "")
    if first(query, "encryption"):
        node["encryption"] = first(query, "encryption")
    if first(query, "flow"):
        node["flow"] = first(query, "flow")
    tls_options(node, query)
    apply_network(node, first(query, "type"), first(query, "host"), first(query, "path"))
    return node

def base_node(proxy_type: str, parsed: urllib.parse.SplitResult, index: int) -> dict[str, Any]:
    host = parsed.hostname or ""
    port = int(parsed.port or 0)
    if not host or not port:
        raise RuntimeError(f"invalid {proxy_type} node at index {index}")
    name = urllib.parse.unquote(parsed.fragment or f"{proxy_type}-{index:03d}")
    return {"name": sanitize_name(name), "type": proxy_type, "server": host, "port": port, "udp": True}

def tls_options(node: dict[str, Any], query: dict[str, list[str]]) -> None:
    sni = first(query, "sni") or first(query, "peer") or first(query, "servername")
    if sni:
        node["sni"] = sni
        node["servername"] = sni
    if first(query, "alpn"):
        node["alpn"] = [item for item in first(query, "alpn").split(",") if item]
    insecure = first(query, "allowInsecure") or first(query, "skip-cert-verify")
    if insecure and insecure.lower() in {"1", "true", "yes"}:
        node["skip-cert-verify"] = True
    fingerprint = first(query, "fp") or first(query, "fingerprint") or first(query, "client-fingerprint")
    node["client-fingerprint"] = fingerprint or "chrome"

def apply_network(node: dict[str, Any], network: str, host: str, path: str) -> None:
    if not network:
        return
    node["network"] = network
    if network == "ws":
        node["ws-opts"] = {"path": path or "/", "headers": {"Host": host}} if host else {"path": path or "/"}
    if network == "grpc":
        node["grpc-opts"] = {"grpc-service-name": path.lstrip("/")}

def first(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or []
    return urllib.parse.unquote(values[0]) if values else ""

def sanitize_name(raw: Any) -> str:
    name = str(raw or "").strip()[:80]
    return name or "proxy-node"

def write_configs(nodes: list[ProxyNode]) -> None:
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for node in nodes:
        (CONFIG_DIR / f"{node.container_name}.yaml").write_text(mihomo_config(node), encoding="utf-8")

def mihomo_config(node: ProxyNode) -> str:
    payload = {
        "mixed-port": MIXED_PORT,
        "allow-lan": True,
        "bind-address": "*",
        "mode": "rule",
        "log-level": "warning",
        "ipv6": False,
        "proxies": [node.config],
        "proxy-groups": [{"name": "AUTO", "type": "select", "proxies": [node.name]}],
        "rules": ["MATCH,AUTO"],
    }
    return to_yaml(payload)

def to_yaml(value: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(value, dict):
        return "".join(yaml_key(pad, key, item, indent) for key, item in value.items())
    if isinstance(value, list):
        return "".join(yaml_list_item(pad, item, indent) for item in value)
    return f"{yaml_scalar(value)}\n"

def yaml_key(pad: str, key: str, value: Any, indent: int) -> str:
    if isinstance(value, (dict, list)):
        return f"{pad}{key}:\n{to_yaml(value, indent + 2)}"
    return f"{pad}{key}: {yaml_scalar(value)}\n"

def yaml_list_item(pad: str, item: Any, indent: int) -> str:
    if isinstance(item, dict):
        parts = list(item.items())
        first_key, first_value = parts[0]
        head = f"{pad}- {first_key}: {yaml_scalar(first_value)}\n"
        return head + "".join(yaml_key(" " * (indent + 2), key, value, indent + 2) for key, value in parts[1:])
    if isinstance(item, list):
        return f"{pad}-\n{to_yaml(item, indent + 2)}"
    return f"{pad}- {yaml_scalar(item)}\n"

def yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)

def healthy_indexes() -> list[int]:
    raw = os.getenv("CLASH_HEALTHY_INDEXES", "").strip()
    indexes = [int(item) for item in raw.split(",") if item.strip()]
    if not indexes:
        raise RuntimeError("CLASH_HEALTHY_INDEXES is required for apply_db phase")
    return indexes

def active_accounts(session) -> list[TgAccount]:
    statement = (
        select(TgAccount)
        .where(TgAccount.tenant_id == 1, TgAccount.deleted_at.is_(None), TgAccount.status == AccountStatus.ACTIVE.value)
        .where(TgAccount.session_ciphertext.is_not(None), TgAccount.account_identity != "code_receiver")
        .order_by(TgAccount.id.asc())
    )
    return list(session.scalars(statement))

def target_group(session, query: str) -> OperationTarget:
    like = f"%{query}%"
    statement = (
        select(OperationTarget)
        .where(OperationTarget.tenant_id == 1, OperationTarget.target_type == "group", OperationTarget.title.ilike(like))
        .order_by(OperationTarget.can_send.desc(), OperationTarget.member_count.desc(), OperationTarget.id.asc())
    )
    target = session.scalar(statement.limit(1))
    if not target:
        raise RuntimeError(f"operation target not found for query={query!r}")
    return target

def upsert_proxy(session, node: ProxyNode, capacity: int) -> AccountProxy:
    proxy = session.scalar(select(AccountProxy).where(AccountProxy.tenant_id == 1, AccountProxy.name == node.container_name))
    if not proxy:
        proxy = AccountProxy(tenant_id=1, name=node.container_name, port=MIXED_PORT)
        session.add(proxy)
        session.flush()
    proxy.protocol = "socks5"
    proxy.host = node.container_name
    proxy.port = MIXED_PORT
    proxy.status = "healthy"
    proxy.alert_status = "normal"
    proxy.max_bound_accounts = capacity
    proxy.max_concurrent_sessions = 2
    proxy.last_check_at = _now()
    proxy.last_error = ""
    proxy.notes = "airport_clash live egress verified by GitHub Actions"
    return proxy

def bind_account(session, account: TgAccount, proxy: AccountProxy) -> None:
    previous_proxy_id = int(account.proxy_id or 0)
    account.proxy_id = proxy.id
    for binding in session.scalars(select(AccountProxyBinding).where(AccountProxyBinding.tenant_id == 1, AccountProxyBinding.account_id == account.id, AccountProxyBinding.status == "active")):
        binding.status = "replaced"
        binding.unbound_at = _now()
    session.add(AccountProxyBinding(tenant_id=1, account_id=account.id, proxy_id=proxy.id, status="active", change_reason="airport_clash_live_binding", bound_by="github-actions"))
    session.add(ProxyHealthCheck(tenant_id=1, proxy_id=proxy.id, check_type="egress_curl", status="success", checked_by="github-actions", checked_at=_now()))
    for auth in session.scalars(select(TgAccountAuthorization).where(TgAccountAuthorization.tenant_id == 1, TgAccountAuthorization.account_id == account.id, TgAccountAuthorization.disabled_at.is_(None))):
        auth.proxy_id = proxy.id
        auth.updated_at = _now()
    audit(session, tenant_id=1, actor="github-actions", action="绑定 Clash 代理", target_type="account", target_id=account.id, detail=f"{previous_proxy_id}->{proxy.id}")

def apply_database(nodes: list[ProxyNode]) -> dict[str, Any]:
    healthy = set(healthy_indexes())
    selected = [node for node in nodes if node.index in healthy]
    if not selected:
        raise RuntimeError("no healthy proxy nodes selected")
    with SessionLocal() as session:
        accounts = active_accounts(session)
        if not accounts:
            raise RuntimeError("no active accounts with sessions")
        capacity = max(1, math.ceil(len(accounts) / len(selected)))
        proxies = [upsert_proxy(session, node, capacity) for node in selected]
        for offset, account in enumerate(accounts):
            bind_account(session, account, proxies[offset % len(proxies)])
        task = create_zhengzhou_task(session, accounts)
        session.commit()
        return summary_payload(accounts, proxies, task, session)

def preflight_database(nodes: list[ProxyNode]) -> dict[str, Any]:
    healthy = set(healthy_indexes())
    selected = [node for node in nodes if node.index in healthy]
    if not selected:
        raise RuntimeError("no healthy proxy nodes selected")
    with SessionLocal() as session:
        accounts = active_accounts(session)
        count = test_account_count()
        if len(accounts) < count:
            raise RuntimeError(f"need exactly {count} test accounts, got {len(accounts)}")
        target = target_group(session, target_query())
        require_protocol_sample(session, search_bot_username())
        return {"account_count": len(accounts), "proxy_count": len(selected), "target_id": target.id, "target_title": target.title}

def create_zhengzhou_task(session, accounts: list[TgAccount]) -> Task:
    count = test_account_count()
    query = target_query()
    bot = search_bot_username()
    target = target_group(session, query)
    selected_ids = [account.id for account in accounts[:count]]
    if len(selected_ids) != count:
        raise RuntimeError(f"need exactly {count} test accounts, got {len(selected_ids)}")
    require_protocol_sample(session, bot)
    payload = SearchJoinGroupTaskCreate(
        name=f"{query}搜索入群线上测试-{len(selected_ids)}账号",
        priority=1,
        account_config=AccountConfig(selection_mode="manual", account_ids=selected_ids, max_concurrent=len(selected_ids), cooldown_per_account_minutes=0),
        target_operation_target_id=target.id,
        target_title=target.title,
        search_bots=[SearchJoinBotConfig(username=bot, display_name=bot)],
        keywords=[query],
        business_region=query,
        proxy_country="AUTO",
        actions_per_round=len(selected_ids),
        max_actions_per_hour=len(selected_ids),
        hourly_min_successful_joins=len(selected_ids),
    )
    _assert_precheck_allows_start(session, 1, "search_join_group", payload.model_dump(mode="json"))
    task = _new_task(session, 1, "search_join_group", payload)
    audit(session, tenant_id=1, actor="github-actions", action="创建任务中心任务", target_type="task", target_id=task.id, detail=task.type)
    _mark_task_started(task)
    audit(session, tenant_id=1, actor="github-actions", action="启动任务中心任务", target_type="task", target_id=task.id)
    created = build_task_plan(session, task)
    if created != count:
        raise RuntimeError(f"search_join action count mismatch: expected={count}, created={created}")
    return task

def test_account_count() -> int:
    count = env_int("CLASH_TEST_ACCOUNT_COUNT", 3)
    if count <= 0:
        raise RuntimeError("CLASH_TEST_ACCOUNT_COUNT must be positive")
    return count

def target_query() -> str:
    return os.getenv("CLASH_TARGET_QUERY", DEFAULT_TARGET_QUERY).strip() or DEFAULT_TARGET_QUERY
def search_bot_username() -> str:
    return os.getenv("CLASH_SEARCH_BOT_USERNAME", DEFAULT_SEARCH_BOT).strip().lstrip("@") or DEFAULT_SEARCH_BOT

def require_protocol_sample(session, bot: str) -> None:
    if env_bool("CLASH_SEED_PROTOCOL_SAMPLE", False):
        seed_protocol_sample(session, bot)
    sample_id = session.scalar(
        select(BotProtocolSample.id).where(
            BotProtocolSample.tenant_id == 1,
            BotProtocolSample.bot_username == bot,
            BotProtocolSample.sample_type == "search_results",
            BotProtocolSample.is_active.is_(True),
            BotProtocolSample.pii_scrubbed.is_(True),
        )
    )
    if not sample_id:
        raise RuntimeError(f"search_join protocol sample missing: {bot}")

def seed_protocol_sample(session, bot: str) -> None:
    sample = session.scalar(
        select(BotProtocolSample).where(
            BotProtocolSample.tenant_id == 1,
            BotProtocolSample.bot_username == bot,
            BotProtocolSample.sample_type == "search_results",
        )
    )
    if not sample:
        sample = BotProtocolSample(tenant_id=1, bot_username=bot, sample_type="search_results")
        session.add(sample)
    sample.sample_hash = "manual-jisou-search-results-v1"
    sample.schema_version = "v1"
    sample.structure_json = {"source": "github-actions-manual-prerequisite", "buttons": [{"type": "telegram_internal_url", "effect": "join_candidate"}]}
    sample.pii_scrubbed = True
    sample.is_active = True
    sample.captured_at = _now()
    session.flush()
    audit(session, tenant_id=1, actor="github-actions", action="补齐搜索机器人协议样本", target_type="bot_protocol_sample", target_id=bot)
def summary_payload(accounts: list[TgAccount], proxies: list[AccountProxy], task: Task, session) -> dict[str, Any]:
    action_count = session.scalar(
        select(func.count(Action.id)).where(Action.tenant_id == 1, Action.task_id == task.id, Action.action_type == "search_join")
    )
    return {
        "account_count": len(accounts),
        "proxy_count": len(proxies),
        "test_task_id": task.id,
        "test_task_status": task.status,
        "test_task_last_error": task.last_error,
        "search_join_action_count": int(action_count or 0),
    }

def phase_prepare() -> None:
    nodes = parsed_nodes(fetch_subscription(subscription_url()), env_int("CLASH_NODE_LIMIT", 64))
    write_configs(nodes)
    print("CLASH_CONFIG_PREPARED=" + json.dumps({"config_dir": str(CONFIG_DIR), "node_count": len(nodes)}, ensure_ascii=False, sort_keys=True))

def phase_apply_db() -> None:
    nodes = parsed_nodes(fetch_subscription(subscription_url()), env_int("CLASH_NODE_LIMIT", 64))
    result = apply_database(nodes)
    print("CLASH_DB_APPLIED=" + json.dumps(result, ensure_ascii=False, sort_keys=True))

def phase_preflight_db() -> None:
    nodes = parsed_nodes(fetch_subscription(subscription_url()), env_int("CLASH_NODE_LIMIT", 64))
    result = preflight_database(nodes)
    print("CLASH_DB_PREFLIGHT_OK=" + json.dumps(result, ensure_ascii=False, sort_keys=True))

def main() -> None:
    phase = os.getenv("CLASH_SETUP_PHASE", "prepare_configs")
    started_at = datetime.now(timezone(timedelta(hours=8))).isoformat()
    print("CLASH_SEARCH_JOIN_PHASE_START=" + json.dumps({"phase": phase, "started_at": started_at}, sort_keys=True), flush=True)
    if phase == "prepare_configs":
        phase_prepare()
        return
    if phase == "apply_db":
        if not env_bool("CLASH_LIVE_APPLY", False):
            raise RuntimeError("CLASH_LIVE_APPLY=true is required for apply_db phase")
        phase_apply_db()
        return
    if phase == "preflight_db":
        phase_preflight_db()
        return
    raise RuntimeError(f"unknown CLASH_SETUP_PHASE={phase}")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("CLASH_SEARCH_JOIN_ERROR=" + json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr, flush=True)
        raise
