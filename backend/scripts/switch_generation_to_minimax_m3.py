"""Apply the approved M3 generator / M2.5 reviewer route revision atomically."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os

from sqlalchemy import func, select, text

from app.database import SessionLocal
from app.models import AiProvider, AuditLog, TenantAiProviderRouteItem, TenantAiProviderRouteSet
from app.services._common import _now
from app.services.task_center.ai_provider_routes import (
    ANTIGRAVITY_GENERATION_PURPOSE_ORDER, active_route_snapshot, resolve_request_route,
)
from app.services.task_center.ai_v2_canary_bootstrap import _apply_routes, _lock_bootstrap_scope
from app.services.task_center.ai_v2_canary_bootstrap_contract import BootstrapChoices, RouteItemChoice


TENANT_ID = 1
GENERATOR_ID = 5
REVIEWER_ID = 4
GENERATION_PURPOSES = (*ANTIGRAVITY_GENERATION_PURPOSE_ORDER,
    "comment_context_route", "comment_realize_general")
REVIEW_PURPOSES = ("group_semantic_review", "comment_semantic_review")
PURPOSES = (*GENERATION_PURPOSES, *REVIEW_PURPOSES)
MODELS = {4: "MiniMax-M2.5", 5: "MiniMax-M3",
    7: "gemini-3.1-pro-low", 8: "gemini-3.6-flash-medium"}
SLOT_URL = "http://host.docker.internal:18101"


@dataclass(frozen=True, kw_only=True)
class CutoverOptions:
    deployed_sha: str
    generator_probe_hash: str
    reviewer_probe_hash: str
    actor: str
    approval_ref: str
    apply: bool = False
    expected_hash: str = ""


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def row_hash(row) -> str:
    return digest({column.key: getattr(row, column.key) for column in row.__table__.columns})


def _rows(session, model, *, predicate, lock):
    statement = select(model).where(predicate).order_by(model.id)
    return list(session.scalars(statement.with_for_update(nowait=True) if lock else statement))


def snapshot(session, options: CutoverOptions) -> dict:
    if os.environ.get("RELEASE_SHA") != options.deployed_sha:
        raise RuntimeError("minimax_cutover_release_changed")
    providers = _rows(session, AiProvider,
        predicate=AiProvider.id.in_(MODELS), lock=options.apply)
    _validate_providers(providers, options)
    routes = _rows(session, TenantAiProviderRouteSet,
        predicate=(TenantAiProviderRouteSet.tenant_id == TENANT_ID)
        & TenantAiProviderRouteSet.purpose.in_(PURPOSES)
        & (TenantAiProviderRouteSet.status == "active"), lock=options.apply)
    if len(routes) != len(PURPOSES) or {route.purpose for route in routes} != set(PURPOSES):
        raise RuntimeError("minimax_cutover_active_routes_incomplete")
    items = _rows(session, TenantAiProviderRouteItem,
        predicate=TenantAiProviderRouteItem.route_set_id.in_([route.id for route in routes]),
        lock=options.apply)
    route_rows = [_route_row(session, route, items=items) for route in routes]
    proposed = _proposed_items(route_rows)
    return {"deployed_sha": options.deployed_sha, "tenant_id": TENANT_ID,
        "providers": [{"id": provider.id, "row_hash": row_hash(provider),
            "health": provider.health_status} for provider in providers],
        "routes": route_rows, "proposed": proposed}


def _validate_providers(providers, options):
    if {provider.id for provider in providers} != set(MODELS):
        raise RuntimeError("minimax_cutover_provider_missing")
    for provider in providers:
        if provider.model_name != MODELS[provider.id] or not provider.credential_enabled or not provider.is_active:
            raise RuntimeError("minimax_cutover_provider_identity_changed")
        if provider.id in (7, 8) and (provider.provider_type != "antigravity_cli"
                or provider.base_url.rstrip("/") != SLOT_URL):
            raise RuntimeError("minimax_cutover_quota_slot_mismatch")
    by_id = {provider.id: provider for provider in providers}
    if by_id[GENERATOR_ID].health_status != "健康":
        raise RuntimeError("minimax_cutover_generator_not_healthy")
    if row_hash(by_id[GENERATOR_ID]) != options.generator_probe_hash:
        raise RuntimeError("minimax_cutover_generator_probe_drift")
    if row_hash(by_id[REVIEWER_ID]) != options.reviewer_probe_hash:
        raise RuntimeError("minimax_cutover_reviewer_probe_drift")


def _route_row(session, route, *, items):
    maximum = session.scalar(select(func.max(TenantAiProviderRouteSet.revision)).where(
        TenantAiProviderRouteSet.tenant_id == TENANT_ID,
        TenantAiProviderRouteSet.purpose == route.purpose))
    values = sorted((item for item in items if item.route_set_id == route.id),
        key=lambda item: item.priority)
    return {"purpose": route.purpose, "id": route.id, "max_revision": maximum,
        "revision": route.revision, "content_hash": route.content_hash,
        "row_hash": row_hash(route), "item_hashes": [row_hash(item) for item in values],
        "items": [{"priority": item.priority, "provider_id": item.provider_id,
            "model_name": item.model_name, "timeout_ms": item.timeout_ms,
            "rate_policy": item.rate_policy, "concurrency_policy": item.concurrency_policy,
            "enabled": item.enabled} for item in values]}


def _template(routes, *, purpose, provider_id):
    route, = (row for row in routes if row["purpose"] == purpose)
    item, = (row for row in route["items"] if row["provider_id"] == provider_id and row["enabled"])
    if item["model_name"] != MODELS[provider_id]:
        raise RuntimeError("minimax_cutover_template_model_mismatch")
    return {key: value for key, value in item.items() if key != "enabled"}


def _proposed_items(routes):
    generator = _template(routes, purpose="group_realize_adult_product", provider_id=GENERATOR_ID)
    reviewer = _template(routes, purpose="group_realize_general", provider_id=REVIEWER_ID)
    return {purpose: [{**(reviewer if purpose in REVIEW_PURPOSES else generator), "priority": 1}]
        for purpose in PURPOSES}


def apply_cutover(session, options: CutoverOptions) -> dict:
    if not options.apply or not options.actor.strip() or not options.approval_ref.strip():
        raise RuntimeError("minimax_cutover_approval_required")
    _lock_bootstrap_scope(session, TENANT_ID)
    before = snapshot(session, options)
    fingerprint = digest(before)
    if fingerprint != options.expected_hash:
        raise RuntimeError("minimax_cutover_preview_drift")
    _update_observed_health(session)
    choices = BootstrapChoices(approver=options.actor, approval_ref=options.approval_ref,
        route_items=tuple((purpose, tuple(RouteItemChoice(**item) for item in items))
            for purpose, items in before["proposed"].items()))
    _apply_routes(session, TENANT_ID, choices=choices, preview=before)
    after = readback_routes(session)
    audit = AuditLog(tenant_id=TENANT_ID, actor=options.actor,
        action="switch_generation_to_minimax_m3", target_type="tenant_ai_provider_routes",
        target_id=str(TENANT_ID), detail=json.dumps({"approval_ref": options.approval_ref,
            "deployed_sha": options.deployed_sha, "before_hash": fingerprint,
            "generator_probe_hash": options.generator_probe_hash,
            "reviewer_probe_hash": options.reviewer_probe_hash,
            "old_routes": [{key: row[key] for key in (
                "id", "purpose", "revision", "content_hash", "item_hashes")} for row in before["routes"]],
            "after": after, "task_mutations": 0, "job_mutations": 0,
            "provider_calls": 0, "telegram_calls": 0}, sort_keys=True))
    session.add(audit)
    session.flush()
    return {"applied": True, "audit_id": audit.id, "after": after}


def _update_observed_health(session):
    for provider_id in (REVIEWER_ID, 7, 8):
        provider = session.get(AiProvider, provider_id)
        provider.health_status = "健康" if provider_id == REVIEWER_ID else "异常"
        provider.last_error = "" if provider_id == REVIEWER_ID else (
            "antigravity_quota_limited: shared Gemini weekly remaining=0; CLI /usage 2026-09-06")
        provider.last_check_at = _now()
        provider.updated_at = _now()
    session.flush()


def readback_routes(session) -> list[dict]:
    result = []
    for purpose in PURPOSES:
        route = active_route_snapshot(session, TENANT_ID, purpose)
        expected_id = REVIEWER_ID if purpose in REVIEW_PURPOSES else GENERATOR_ID
        if route.provider_ids != (expected_id,) or route.provider_models != {expected_id: MODELS[expected_id]}:
            raise RuntimeError("minimax_cutover_readback_mismatch")
        result.append({"purpose": purpose, "id": route.route_set_id,
            "revision": route.revision, "content_hash": route.content_hash,
            "provider_ids": route.provider_ids})
    for scope in ("group", "comment"):
        resolve_request_route(session, TENANT_ID, "两阶段语义审核",
            config={"ai_content_route_v2_enabled": True, "_ai_content_scope_type": scope})
    return result


def run(options: CutoverOptions, session_factory=SessionLocal) -> dict:
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            if not options.apply:
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            session.execute(text("SET LOCAL lock_timeout='2s'"))
            session.execute(text("SET LOCAL statement_timeout='12s'"))
        if not options.apply:
            before = snapshot(session, options)
            return {"mode": "preview", "fingerprint": digest(before), **before}
        result = apply_cutover(session, options)
        session.commit()
        return result


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("deployed-sha", "generator-probe-hash", "reviewer-probe-hash", "actor", "approval-ref"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-hash", default="")
    options = CutoverOptions(**vars(parser.parse_args()))
    print(json.dumps(run(options), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
