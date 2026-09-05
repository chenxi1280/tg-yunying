"""Read-only quantity and shared-budget evidence, without freezing a new plan."""
from collections import defaultdict
import math
from types import SimpleNamespace

from app.common.state_hash import canonical_state_hash
from app.services._common import _now
from app.services.account_group_revision_bootstrap import preview_group_revisions
from app.timezone import as_beijing

from .channel_source_intake import _known_messages, initial_source_keys
from .channel_source_policy import logical_source_key, source_window_end
from .config_normalization import validated_type_config
from .engagement_policy_scope import policy_eligible_member_ids
from .engagement_portfolio_capacity import read_portfolio_capacities


ACTION_CLASSES = {"group_ai_chat": "authored_message", "channel_comment": "authored_comment",
    "channel_like": "reaction", "channel_view": "view"}
BASIS_POINTS = 10_000


def preview_cutover_capacity(session, preview):
    spec = preview["state"]["spec"]
    groups = {row["pool_id"]: row for row in preview_group_revisions(session, spec["tenant_id"])["state"]["groups"]}
    scopes = [_scope(session, item["old"], spec["replacements"][item["old"]["id"]], groups=groups)
        for item in preview["state"]["tasks"]]
    now = as_beijing(_now())
    classes = defaultdict(list)
    for scope in scopes:
        classes[ACTION_CLASSES[scope[0].type]].append(scope)
    rows, shared = [], []
    for action_class, selected in sorted(classes.items()):
        account_ids = sorted({key for _, members in selected for key in members})
        capacities, policy_ids = read_portfolio_capacities(session, spec["tenant_id"], now.date(),
            account_ids=account_ids, action_class=action_class, lock_policies=False)
        demands = [_quantity_row(session, task, members, now=now, capacities=capacities) for task, members in selected]
        upper = sum(row["initial_demand_upper_bound"] for row in demands)
        shared.append({"action_class": action_class, "member_union_count": len(account_ids),
            "available_units": sum(capacities.values()), "policy_ids": policy_ids,
            "initial_demand_upper_bound": upper,
            "class_budget_deficit_lower_bound": max(0, upper - sum(capacities.values()))})
        rows.extend(demands)
    return {"observed_at": now.isoformat(), "task_day": now.date().isoformat(), "tasks": rows,
        "shared_class_budgets": shared, "planning_admission": "not_frozen",
        "future_sources": "conditional", "legacy_call_occupancy": "retained_at_runtime_admission"}


def _scope(session, old, overrides, *, groups):
    config = validated_type_config(old["type"], {**old["type_config"], **overrides})
    selected = [groups[key] for key in config["account_group_ids"]]
    snapshot = SimpleNamespace(member_account_ids=sorted(member["account_id"] for group in selected
        for member in group["member_contracts"]), group_memberships=selected)
    task = SimpleNamespace(id=old["id"], tenant_id=old.get("tenant_id", selected[0]["state"]["tenant_id"]),
        type=old["type"], type_config=config, pacing_config=old["pacing_config"],
        stats={}, scheduled_start=None, created_at=_now())
    return task, policy_eligible_member_ids(session, task, snapshot)


def _quantity_row(session, task, members, *, now, capacities):
    config = task.type_config
    n = len(members)
    source_count, source_hash = _sources(session, task, now=now)
    low, high = _quantity_bounds(task.type, config, members=n)
    multiplier = 1 if task.type == "group_ai_chat" else source_count
    if task.type == "channel_view":
        multiplier = min(source_count, int(config["per_account_source_degree_max"]))
    return {"old_task_id": task.id, "type": task.type, "stable_members": n,
        "member_hash": canonical_state_hash(list(members)), "initial_active_source_count": source_count,
        "initial_source_hash": source_hash, "quantity_lower_bound": low, "quantity_upper_bound": high,
        "initial_demand_upper_bound": high * multiplier,
        "available_class_units": sum(capacities[key] for key in members),
        "configured_daily_cap": config.get("daily_comment_cap", config.get("daily_reaction_cap")),
        "source_observation_complete": False}


def _sources(session, task, *, now):
    if task.type == "group_ai_chat":
        return 1, ""
    messages = _known_messages(session, task, task.type_config)
    keys, _ = initial_source_keys(task, messages, task.type_config, anchor=now)
    active = sorted({logical_source_key(message) for message in messages
        if logical_source_key(message) in keys and source_window_end(task, message) > now})
    return len(active), canonical_state_hash(active)


def _quantity_bounds(task_type, config, *, members):
    if task_type == "group_ai_chat":
        target = int(config["daily_message_target"])
        jitter = int(config.get("daily_target_jitter_bps", 0)) / BASIS_POINTS
        return math.floor(target * (1 - jitter)), math.ceil(target * (1 + jitter))
    if task_type == "channel_like":
        target, jitter = int(config["target_likes_per_message"]), float(config["like_count_jitter"])
        return math.floor(target * (1 - jitter)), math.ceil(target * (1 + jitter))
    low = (members * int(config["account_ratio_min_bps"]) + BASIS_POINTS // 2) // BASIS_POINTS
    high = (members * int(config["account_ratio_max_bps"]) + BASIS_POINTS // 2) // BASIS_POINTS
    return low, high
