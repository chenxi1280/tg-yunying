#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


TASK_ACTIONS_PER_MINUTE = {
    "group_ai_chat": 2.4,
    "group_relay": 3.0,
    "channel_view": 6.0,
    "channel_like": 4.0,
    "channel_comment": 2.0,
}

GATEWAY_MODES = {
    "fast": {"latency_seconds": 0.25, "success_ratio": 0.99, "unknown_ratio": 0.000, "limited_ratio": 0.000},
    "slow": {"latency_seconds": 1.20, "success_ratio": 0.96, "unknown_ratio": 0.001, "limited_ratio": 0.000},
    "flood_wait": {"latency_seconds": 0.80, "success_ratio": 0.88, "unknown_ratio": 0.001, "limited_ratio": 0.080},
    "slowmode": {"latency_seconds": 0.60, "success_ratio": 0.90, "unknown_ratio": 0.001, "limited_ratio": 0.060},
    "unknown_after_send": {"latency_seconds": 0.75, "success_ratio": 0.94, "unknown_ratio": 0.010, "limited_ratio": 0.000},
}


@dataclass(frozen=True)
class Scenario:
    accounts: int
    tasks: int


@dataclass(frozen=True)
class CapacityResult:
    scenario: str
    gateway_mode: str
    duration_minutes: int
    generated_actions: int
    processed_actions: int
    backlog_actions: int
    throughput_per_minute: float
    oldest_pending_p95_seconds: float
    unknown_after_send: int
    duplicate_sends: int
    recommended_dispatcher_workers: int
    recommended_dispatcher_concurrency: int
    recommended_action_claim_limit: int
    recommended_db_pool_size: int
    recommended_db_max_overflow: int
    single_node_boundary_accounts: int
    bottleneck: str
    notes: list[str]


def parse_scenario(raw: str) -> Scenario:
    try:
        accounts_raw, tasks_raw = raw.split(":", 1)
        scenario = Scenario(accounts=int(accounts_raw), tasks=int(tasks_raw))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("scenario must use ACCOUNTS:TASKS, for example 1000:30") from exc
    if scenario.accounts <= 0 or scenario.tasks <= 0:
        raise argparse.ArgumentTypeError("scenario accounts and tasks must be positive")
    return scenario


def task_mix_actions_per_minute(tasks: int) -> float:
    task_types = list(TASK_ACTIONS_PER_MINUTE)
    total = 0.0
    for index in range(tasks):
        total += TASK_ACTIONS_PER_MINUTE[task_types[index % len(task_types)]]
    return total


def recommend_dispatchers(accounts: int, tasks: int) -> int:
    if accounts <= 120 and tasks <= 5:
        return 2
    if accounts <= 400 and tasks <= 12:
        return 4
    return 8


def recommended_concurrency(accounts: int, workers: int, db_pool_size: int, db_max_overflow: int) -> int:
    connection_budget = max(1, db_pool_size + db_max_overflow - 2)
    by_accounts = max(4, math.ceil(accounts / max(1, workers) / 12))
    return max(4, min(40, by_accounts, connection_budget))


def simulate(
    scenario: Scenario,
    *,
    gateway_mode: str,
    duration_minutes: int,
    db_pool_size: int,
    db_max_overflow: int,
    global_tg_rate_per_second: float,
    account_rate_per_hour: int,
) -> CapacityResult:
    mode = GATEWAY_MODES[gateway_mode]
    workers = recommend_dispatchers(scenario.accounts, scenario.tasks)
    concurrency = recommended_concurrency(scenario.accounts, workers, db_pool_size, db_max_overflow)
    demand_per_minute = task_mix_actions_per_minute(scenario.tasks)
    generated = int(math.ceil(demand_per_minute * duration_minutes))

    raw_worker_capacity = workers * concurrency * (60 / mode["latency_seconds"])
    global_capacity = global_tg_rate_per_second * 60
    account_capacity = scenario.accounts * account_rate_per_hour / 60
    processed_per_minute = min(raw_worker_capacity, global_capacity, account_capacity, demand_per_minute)
    attempted = int(math.floor(processed_per_minute * duration_minutes))
    successful = int(math.floor(attempted * mode["success_ratio"]))
    unknown = int(math.floor(attempted * mode["unknown_ratio"]))
    limited = int(math.floor(attempted * mode["limited_ratio"]))
    backlog = max(0, generated - attempted)
    oldest_pending = 0.0 if backlog == 0 else min(duration_minutes * 60.0, backlog / max(1.0, processed_per_minute) * 60.0)

    capacity_map = {
        "worker": raw_worker_capacity,
        "global_tg_rate": global_capacity,
        "account_capacity": account_capacity,
        "generated_demand": demand_per_minute,
    }
    bottleneck = min(capacity_map, key=capacity_map.get)
    notes = [
        f"successful_actions={successful}",
        f"limited_or_delayed_actions={limited}",
        "duplicate_sends fixed at 0 because the model assumes Action claim + lease + Redis account lock are enabled",
    ]
    if backlog:
        notes.append("backlog grows in this model; increase accounts/rate limit/dispatcher budget before promising this scenario")
    if concurrency >= db_pool_size + db_max_overflow - 2:
        notes.append("dispatcher concurrency is capped by PostgreSQL connection budget")

    single_node_boundary = max(50, int(min(global_capacity, raw_worker_capacity) / max(1, account_rate_per_hour / 60)))
    return CapacityResult(
        scenario=f"{scenario.accounts}:{scenario.tasks}",
        gateway_mode=gateway_mode,
        duration_minutes=duration_minutes,
        generated_actions=generated,
        processed_actions=attempted,
        backlog_actions=backlog,
        throughput_per_minute=round(attempted / duration_minutes, 2),
        oldest_pending_p95_seconds=round(oldest_pending, 2),
        unknown_after_send=unknown,
        duplicate_sends=0,
        recommended_dispatcher_workers=workers,
        recommended_dispatcher_concurrency=concurrency,
        recommended_action_claim_limit=max(50, workers * concurrency * 2),
        recommended_db_pool_size=db_pool_size,
        recommended_db_max_overflow=db_max_overflow,
        single_node_boundary_accounts=single_node_boundary,
        bottleneck=bottleneck,
        notes=notes,
    )


def render_markdown(results: list[CapacityResult]) -> str:
    lines = [
        "# TG 运营管理平台容量报告：100 / 300 / 1000 账号",
        "",
        f"> 生成时间：{datetime.now(timezone.utc).isoformat()}",
        "> 口径：本报告由 mock gateway 容量模型生成，用于固化压测参数和风险边界；不是线上 Telegram API 实测结论。",
        "",
        "## 结论",
        "",
        "- 100 账号 / 5 任务：按当前角色拆分和连接池预算，可以作为试运行容量。",
        "- 300 账号 / 10 任务：需要 4 个 Dispatcher worker，并保持 Redis token bucket 与账号 in-flight 开启。",
        "- 1000 账号 / 20-30 任务：需要 8 个 Dispatcher worker 起步，且必须用真实 PostgreSQL / Redis 压测复核 TG 延迟、限流和 backlog。",
        "- 重复发送验收口径为 `duplicate_sends = 0`；如果真实压测出现非 0，必须先修 claim / lease / account lock。",
        "",
        "## 明细",
        "",
        "| 场景 | Gateway | 生成 Action | 处理 Action | 吞吐/分钟 | Backlog | Oldest pending P95 | unknown_after_send | 重复发送 | Dispatcher | 并发 | Claim limit | DB pool | 瓶颈 | 单机账号边界 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |",
    ]
    for result in results:
        lines.append(
            "| {scenario} | {gateway_mode} | {generated_actions} | {processed_actions} | {throughput_per_minute} | "
            "{backlog_actions} | {oldest_pending_p95_seconds}s | {unknown_after_send} | {duplicate_sends} | "
            "{recommended_dispatcher_workers} | {recommended_dispatcher_concurrency} | {recommended_action_claim_limit} | "
            "{recommended_db_pool_size}+{recommended_db_max_overflow} | {bottleneck} | {single_node_boundary_accounts} |".format(
                **asdict(result)
            )
        )
    lines.extend(
        [
            "",
            "## 验收要求",
            "",
            "- PostgreSQL-only：最终验收必须使用 `postgresql+psycopg://...`，不能用 SQLite 代替。",
            "- Redis 打开：目标场景必须开启 token bucket 和 account in-flight；Redis 不可用场景必须 fail-closed。",
            "- worker 异常退出：应通过 claim/recovery 看到 pending 恢复，不能重复发送。",
            "- DB 紧张场景：Dispatcher 并发必须被连接池预算压住，不能无限调大。",
            "- 真实任务验证：至少创建 AI 活跃、转发监听、频道浏览、频道点赞、频道评论各 1 个任务，确认 Action 创建、claim、执行和 metrics 快照均有记录。",
            "",
            "## 后续实测命令",
            "",
            "```bash",
            "cd /Users/xida/PycharmProjects/tg-yunying/backend",
            "APP_ENV=test \\",
            "DATABASE_URL='postgresql+psycopg://...' \\",
            "TEST_DATABASE_URL='postgresql+psycopg://...' \\",
            "REDIS_URL='redis://...' \\",
            "TG_GATEWAY_MODE=mock \\",
            ".venv/bin/python scripts/run_capacity_benchmark.py \\",
            "  --scenario 100:5 --scenario 300:10 --scenario 1000:30 \\",
            "  --gateway-mode fast,slow,flood_wait,slowmode,unknown_after_send \\",
            "  --output ../reports/capacity/latest.json \\",
            "  --markdown ../docs/capacity-report-100-300-1000.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic TG task-center capacity model.")
    parser.add_argument("--scenario", action="append", type=parse_scenario, required=True)
    parser.add_argument("--gateway-mode", default="fast,slow,flood_wait,slowmode,unknown_after_send")
    parser.add_argument("--duration-minutes", type=int, default=30)
    parser.add_argument("--db-pool-size", type=int, default=20)
    parser.add_argument("--db-max-overflow", type=int, default=20)
    parser.add_argument("--global-tg-rate-per-second", type=float, default=30)
    parser.add_argument("--account-rate-per-hour", type=int, default=120)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args()

    modes = [item.strip() for item in args.gateway_mode.split(",") if item.strip()]
    unknown_modes = sorted(set(modes) - set(GATEWAY_MODES))
    if unknown_modes:
        parser.error(f"unknown gateway mode(s): {', '.join(unknown_modes)}")

    results = [
        simulate(
            scenario,
            gateway_mode=mode,
            duration_minutes=args.duration_minutes,
            db_pool_size=args.db_pool_size,
            db_max_overflow=args.db_max_overflow,
            global_tg_rate_per_second=args.global_tg_rate_per_second,
            account_rate_per_hour=args.account_rate_per_hour,
        )
        for scenario in args.scenario
        for mode in modes
    ]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": {
            "task_actions_per_minute": TASK_ACTIONS_PER_MINUTE,
            "gateway_modes": GATEWAY_MODES,
            "duration_minutes": args.duration_minutes,
            "db_pool_size": args.db_pool_size,
            "db_max_overflow": args.db_max_overflow,
            "global_tg_rate_per_second": args.global_tg_rate_per_second,
            "account_rate_per_hour": args.account_rate_per_hour,
        },
        "results": [asdict(result) for result in results],
    }
    rendered_json = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered_json)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered_json + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(results), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
