from __future__ import annotations

import hashlib
import json
import os

from sqlalchemy import func, select

from app.models import (
    AiProvider,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
    TenantAiSetting,
)
from app.services._common import _now, audit
from app.security import decrypt_secret
from app.services.task_center.ai_provider_routes import (
    ANTIGRAVITY_GENERATION_PURPOSE_ORDER,
)


SCRIPT_VERSION = "ai_provider_failover_v1"
ANTIGRAVITY_MODELS = (
    "gemini-3.5-flash-medium",
    "gemini-3.1-pro-low",
)
ANTIGRAVITY_SLOT_BASE_URL = "http://host.docker.internal:18101"


def run_generation_cutover(options, session_factory) -> dict:  # noqa: ANN001
    apply = options.operation == "generation-cutover-apply"
    with session_factory() as session:
        snapshot = _snapshot(session, options, lock=apply)
        if not apply:
            operation = (
                "generation-readback"
                if options.operation.endswith("readback")
                else "generation-cutover"
            )
            return {**snapshot, "operation": operation}
        _require_fingerprint(snapshot, options.expected_fingerprint)
        _require_ready(snapshot)
        if _is_desired(snapshot):
            return {"applied": False, "noop": True, "readback": snapshot}
        _apply(session, options, snapshot)
        session.commit()
    return {"applied": True, "readback": _readback(options, session_factory)}


def _snapshot(session, options, *, lock: bool) -> dict:  # noqa: ANN001
    runtime_sha = os.environ.get("RELEASE_SHA", "")
    if runtime_sha != options.deployed_sha:
        raise RuntimeError("ai_provider_runtime_sha_mismatch")
    providers = _providers(session, options.provider_ids, lock=lock)
    setting = _setting(session, options.tenant_id, lock=lock)
    routes = [
        _route_snapshot(session, options, providers, purpose, lock=lock)
        for purpose in ANTIGRAVITY_GENERATION_PURPOSE_ORDER
    ]
    return _with_fingerprint({
        "version": SCRIPT_VERSION,
        "operation": "generation-cutover",
        "tenant_id": options.tenant_id,
        "actor": options.actor,
        "approval_ref": options.approval_ref,
        "deployed_sha": options.deployed_sha,
        "runtime_sha": runtime_sha,
        "providers": [provider_snapshot_row(provider) for provider in providers],
        "setting": setting_snapshot_row(setting),
        "desired_default_provider_id": providers[0].id,
        "routes": routes,
    })


def _route_snapshot(
    session,
    options,
    providers: list[AiProvider],
    purpose: str,
    *,
    lock: bool,
) -> dict:  # noqa: ANN001
    active = _active_route(session, options.tenant_id, purpose, lock=lock)
    old_route = _route_row(session, active, lock=lock) if active else None
    old_items = old_route["items"] if old_route else []
    new_ids = {provider.id for provider in providers}
    desired = [
        {"priority": index, "provider_id": provider.id, "model_name": provider.model_name}
        for index, provider in enumerate(providers, 1)
    ]
    for item in old_items:
        if item["provider_id"] not in new_ids:
            desired.append({**item, "priority": len(desired) + 1})
    return {
        "purpose": purpose,
        "old_route": old_route,
        "desired_items": desired,
        "desired_hash": _content_hash(desired),
    }


def _require_ready(snapshot: dict) -> None:
    _require_antigravity_pair(snapshot["providers"])
    invalid = [
        row["id"] for row in snapshot["providers"]
        if not row["credential_enabled"]
        or not row["is_active"]
        or row["health_status"] != "健康"
    ]
    if invalid:
        raise RuntimeError(f"provider_route_not_ready:{','.join(map(str, invalid))}")
    missing = [row["purpose"] for row in snapshot["routes"] if row["old_route"] is None]
    if missing:
        raise RuntimeError(f"generation_route_missing:{','.join(missing)}")
    if any(len(row["desired_items"]) < 3 for row in snapshot["routes"]):
        raise RuntimeError("generation_route_original_fallback_missing")


def _require_antigravity_pair(providers: list[dict]) -> None:
    if len(providers) != 2:
        raise RuntimeError("antigravity_provider_pair_required")
    if tuple(row["model_name"] for row in providers) != ANTIGRAVITY_MODELS:
        raise RuntimeError("antigravity_provider_model_order_invalid")
    if any(row["provider_type"] != "antigravity_cli" for row in providers):
        raise RuntimeError("antigravity_provider_type_invalid")
    if {row["base_url"] for row in providers} != {ANTIGRAVITY_SLOT_BASE_URL}:
        raise RuntimeError("antigravity_provider_slot_mismatch")
    fingerprints = {row["bridge_token_fingerprint"] for row in providers}
    if len(fingerprints) != 1 or "unreadable" in fingerprints:
        raise RuntimeError("antigravity_provider_token_mismatch")


def _is_desired(snapshot: dict) -> bool:
    if snapshot["setting"]["default_provider_id"] != snapshot["desired_default_provider_id"]:
        return False
    return all(
        row["old_route"]["content_hash"] == row["desired_hash"]
        for row in snapshot["routes"]
    )


def _apply(session, options, snapshot: dict) -> None:  # noqa: ANN001
    for row in snapshot["routes"]:
        _replace_route(session, options, row["purpose"], row)
    setting = _setting(session, options.tenant_id, lock=True)
    setting.default_provider_id = snapshot["desired_default_provider_id"]
    setting.ai_provider_route_fallback_enabled = True
    setting.updated_at = _now()
    audit(
        session,
        tenant_id=options.tenant_id,
        actor=options.actor,
        action="原子切换全部AI生成供应商路由",
        target_type="tenant_ai_provider_route_set",
        target_id=str(options.tenant_id),
        detail=options.approval_ref,
    )


def _replace_route(session, options, purpose: str, snapshot: dict):  # noqa: ANN001
    active = _active_route(session, options.tenant_id, purpose, lock=True)
    if active:
        active.status = "retired"
        session.flush()
    revision = int(session.scalar(select(func.max(TenantAiProviderRouteSet.revision)).where(
        TenantAiProviderRouteSet.tenant_id == options.tenant_id,
        TenantAiProviderRouteSet.purpose == purpose,
    )) or 0) + 1
    route = TenantAiProviderRouteSet(
        tenant_id=options.tenant_id,
        purpose=purpose,
        revision=revision,
        status="active",
        content_hash=snapshot["desired_hash"],
        approved_by=options.actor,
        approved_at=_now(),
    )
    session.add(route)
    session.flush()
    for item in snapshot["desired_items"]:
        session.add(TenantAiProviderRouteItem(route_set_id=route.id, **item))
    return route


def _providers(session, provider_ids: tuple[int, ...], *, lock: bool) -> list[AiProvider]:  # noqa: ANN001
    statement = select(AiProvider).where(AiProvider.id.in_(provider_ids))
    if lock:
        statement = statement.with_for_update()
    rows = list(session.scalars(statement))
    by_id = {provider.id: provider for provider in rows}
    missing = [provider_id for provider_id in provider_ids if provider_id not in by_id]
    if missing:
        raise RuntimeError(f"ai_provider_missing:{','.join(map(str, missing))}")
    return [by_id[provider_id] for provider_id in provider_ids]


def _setting(session, tenant_id: int, *, lock: bool):  # noqa: ANN001
    statement = select(TenantAiSetting).where(TenantAiSetting.tenant_id == tenant_id)
    if lock:
        statement = statement.with_for_update()
    setting = session.scalar(statement)
    if setting is None:
        raise RuntimeError(f"tenant_ai_setting_missing:{tenant_id}")
    return setting


def _active_route(session, tenant_id: int, purpose: str, *, lock: bool):  # noqa: ANN001
    statement = select(TenantAiProviderRouteSet).where(
        TenantAiProviderRouteSet.tenant_id == tenant_id,
        TenantAiProviderRouteSet.purpose == purpose,
        TenantAiProviderRouteSet.status == "active",
    )
    if lock:
        statement = statement.with_for_update()
    return session.scalar(statement)


def _route_row(session, route: TenantAiProviderRouteSet, *, lock: bool) -> dict:  # noqa: ANN001
    statement = select(TenantAiProviderRouteItem).where(
        TenantAiProviderRouteItem.route_set_id == route.id,
    ).order_by(TenantAiProviderRouteItem.priority)
    if lock:
        statement = statement.with_for_update()
    items = session.scalars(statement).all()
    return {
        "id": route.id,
        "revision": route.revision,
        "content_hash": route.content_hash,
        "items": [
            {"priority": item.priority, "provider_id": item.provider_id, "model_name": item.model_name}
            for item in items
        ],
    }


def provider_snapshot_row(provider: AiProvider) -> dict:
    return {
        "id": provider.id,
        "provider_name": provider.provider_name,
        "provider_type": provider.provider_type,
        "base_url": provider.base_url,
        "model_name": provider.model_name,
        "credential_fingerprint": hashlib.sha256(
            str(provider.api_key_ciphertext or "").encode("utf-8")
        ).hexdigest(),
        "bridge_token_fingerprint": _bridge_token_fingerprint(provider),
        "credential_enabled": provider.credential_enabled,
        "is_active": provider.is_active,
        "health_status": provider.health_status,
        "last_check_at": provider.last_check_at,
        "last_error": str(provider.last_error or "")[:240],
    }


def setting_snapshot_row(setting: TenantAiSetting) -> dict:
    return {
        "tenant_id": setting.tenant_id,
        "default_provider_id": setting.default_provider_id,
        "ai_provider_route_fallback_enabled": setting.ai_provider_route_fallback_enabled,
    }


def _readback(options, session_factory) -> dict:  # noqa: ANN001
    with session_factory() as session:
        snapshot = _snapshot(session, options, lock=False)
    return {**snapshot, "operation": "generation-readback"}


def _content_hash(items: list[dict]) -> str:
    return hashlib.sha256(_canonical(items)).hexdigest()


def _with_fingerprint(body: dict) -> dict:
    return {**body, "fingerprint": hashlib.sha256(_canonical(body)).hexdigest()}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _require_fingerprint(snapshot: dict, expected: str) -> None:
    if snapshot["fingerprint"] != expected:
        raise RuntimeError("ai_provider_failover_fingerprint_mismatch")


def _bridge_token_fingerprint(provider: AiProvider) -> str:
    try:
        token = decrypt_secret(provider.api_key_ciphertext)
    except Exception:  # noqa: BLE001 - only a fingerprint leaves the boundary.
        return "unreadable"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
