from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    AiProvider,
    AiProviderHealthStatus,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
    TenantAiSetting,
)
from app.services._common import _now, audit
from app.services.ai_config import AI_PROVIDER_RECHECK_REQUIRED, check_ai_provider


DEFAULT_ROUTE_PURPOSE = "group_realize_general"
ROUTE_PURPOSE = DEFAULT_ROUTE_PURPOSE
SCRIPT_VERSION = "ai_provider_failover_v1"


@dataclass(frozen=True)
class Options:
    operation: str
    tenant_id: int
    provider_ids: tuple[int, ...]
    expected_fingerprint: str
    actor: str
    approval_ref: str
    purpose: str = DEFAULT_ROUTE_PURPOSE


def main() -> None:
    options = _options()
    if options.operation == "provider-check":
        result = _check_providers(options)
    elif options.operation.startswith("providers-"):
        result = _providers_operation(options)
    elif options.operation.startswith("default-"):
        result = _default_operation(options)
    elif options.operation.startswith("cutover-"):
        result = _cutover_operation(options)
    else:
        result = _route_operation(options)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))


def _options() -> Options:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=(
        "providers-preview", "providers-apply", "provider-check",
        "default-preview", "default-apply",
        "route-preview", "route-apply", "readback",
        "cutover-preview", "cutover-apply",
    ))
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--provider-id", type=int, action="append", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="codex-production-remediation")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--purpose", default=DEFAULT_ROUTE_PURPOSE)
    args = parser.parse_args()
    provider_ids = tuple(dict.fromkeys(args.provider_id))
    if len(provider_ids) < 2:
        parser.error("at least two distinct --provider-id values are required")
    if args.operation.endswith("apply") and (
        len(args.expected_fingerprint) != 64 or not args.approval_ref
    ):
        parser.error("apply requires --expected-fingerprint and --approval-ref")
    if args.operation == "provider-check" and not args.approval_ref:
        parser.error("provider-check requires --approval-ref")
    return Options(
        args.operation,
        args.tenant_id,
        provider_ids,
        args.expected_fingerprint,
        args.actor,
        args.approval_ref,
        args.purpose,
    )


def _providers_operation(options: Options) -> dict:
    apply = options.operation == "providers-apply"
    with SessionLocal() as session:
        providers = _providers(session, options.provider_ids, lock=apply)
        snapshot = _providers_snapshot(options.tenant_id, providers)
        if not apply:
            return snapshot
        _require_fingerprint(snapshot, options.expected_fingerprint)
        for provider in providers:
            _enable_provider(session, provider, options)
        session.commit()
    return {"applied": True, "readback": _readback(options)}


def _enable_provider(session, provider: AiProvider, options: Options) -> None:  # noqa: ANN001
    changed = not provider.credential_enabled or not provider.is_active
    if not provider.credential_enabled:
        provider.health_status = AiProviderHealthStatus.UNHEALTHY.value
        provider.last_error = AI_PROVIDER_RECHECK_REQUIRED
    provider.credential_enabled = True
    provider.is_active = True
    provider.updated_at = _now()
    if changed:
        audit(
            session,
            tenant_id=options.tenant_id,
            actor=options.actor,
            action="启用AI供应商候选",
            target_type="ai_provider",
            target_id=str(provider.id),
            detail=options.approval_ref,
        )


def _check_providers(options: Options) -> dict:
    results = []
    with SessionLocal() as session:
        _providers(session, options.provider_ids, lock=False)
        for provider_id in options.provider_ids:
            provider = check_ai_provider(session, provider_id, options.actor)
            audit(
                session,
                tenant_id=options.tenant_id,
                actor=options.actor,
                action="AI供应商生产健康检查",
                target_type="ai_provider",
                target_id=str(provider.id),
                detail=options.approval_ref,
            )
            session.commit()
            results.append(_provider_row(provider))
    healthy = [row["id"] for row in results if row["health_status"] == "健康"]
    return {
        "version": SCRIPT_VERSION,
        "operation": "provider-check",
        "approval_ref": options.approval_ref,
        "healthy_provider_ids": healthy,
        "all_healthy": len(healthy) == len(options.provider_ids),
        "providers": results,
    }


def _default_operation(options: Options) -> dict:
    apply = options.operation == "default-apply"
    with SessionLocal() as session:
        snapshot = _default_snapshot(session, options, lock=apply)
        if not apply:
            return snapshot
        _require_fingerprint(snapshot, options.expected_fingerprint)
        _require_default_ready(snapshot)
        setting = _setting(session, options.tenant_id, lock=True)
        old_provider_id = setting.default_provider_id
        setting.default_provider_id = snapshot["desired_default_provider_id"]
        setting.updated_at = _now()
        audit(
            session,
            tenant_id=options.tenant_id,
            actor=options.actor,
            action="切换默认AI供应商",
            target_type="tenant_ai_setting",
            target_id=str(setting.id),
            detail=(
                f"{options.approval_ref};old={old_provider_id};"
                f"new={setting.default_provider_id}"
            ),
        )
        session.commit()
    return {"applied": True, "readback": _default_readback(options)}


def _default_snapshot(session, options: Options, *, lock: bool) -> dict:  # noqa: ANN001
    providers = _providers(session, options.provider_ids, lock=lock)
    setting = _setting(session, options.tenant_id, lock=lock)
    body = {
        "version": SCRIPT_VERSION,
        "operation": "default",
        "tenant_id": options.tenant_id,
        "providers": [_provider_row(provider) for provider in providers],
        "setting": _setting_row(setting),
        "desired_default_provider_id": providers[0].id,
    }
    return _with_fingerprint(body)


def _default_readback(options: Options) -> dict:
    with SessionLocal() as session:
        snapshot = _default_snapshot(session, options, lock=False)
    return {**snapshot, "operation": "readback"}


def _require_default_ready(snapshot: dict) -> None:
    provider = snapshot["providers"][0]
    if (
        not provider["credential_enabled"]
        or not provider["is_active"]
        or provider["health_status"] != "健康"
    ):
        raise RuntimeError(f"default_provider_not_ready:{provider['id']}")


def _route_operation(options: Options) -> dict:
    if options.operation == "readback":
        return _readback(options)
    apply = options.operation == "route-apply"
    with SessionLocal() as session:
        snapshot = _route_snapshot(session, options, lock=apply)
        if not apply:
            return snapshot
        _require_fingerprint(snapshot, options.expected_fingerprint)
        _require_route_ready(snapshot)
        _apply_route(session, options, snapshot)
        session.commit()
    return {"applied": True, "readback": _readback(options)}


def _cutover_operation(options: Options) -> dict:
    apply = options.operation == "cutover-apply"
    with SessionLocal() as session:
        snapshot = _cutover_snapshot(session, options, lock=apply)
        if not apply:
            return snapshot
        _require_fingerprint(snapshot, options.expected_fingerprint)
        _require_cutover_ready(snapshot)
        _apply_cutover(session, options, snapshot)
        session.commit()
    return {"applied": True, "readback": _cutover_readback(options)}


def _cutover_snapshot(session, options: Options, *, lock: bool) -> dict:  # noqa: ANN001
    route = _route_snapshot(session, options, lock=lock)
    route.pop("fingerprint", None)
    return _with_fingerprint({
        **route,
        "operation": "cutover",
        "desired_default_provider_id": route["desired_items"][0]["provider_id"],
    })


def _apply_cutover(session, options: Options, snapshot: dict) -> None:  # noqa: ANN001
    setting = _setting(session, options.tenant_id, lock=True)
    old_provider_id = setting.default_provider_id
    route = _replace_active_route(session, options, snapshot)
    setting.default_provider_id = snapshot["desired_default_provider_id"]
    setting.ai_provider_route_fallback_enabled = True
    setting.updated_at = _now()
    _write_cutover_audit(
        session,
        options,
        route,
        old_provider_id=old_provider_id,
        new_provider_id=setting.default_provider_id,
    )


def _write_cutover_audit(
    session,
    options: Options,
    route: TenantAiProviderRouteSet,
    *,
    old_provider_id: int | None,
    new_provider_id: int,
) -> None:  # noqa: ANN001
    audit(
        session,
        tenant_id=options.tenant_id,
        actor=options.actor,
        action="原子切换默认AI供应商与路由",
        target_type="tenant_ai_provider_route_set",
        target_id=route.id,
        detail=(
            f"{options.approval_ref};old_default={old_provider_id};"
            f"new_default={new_provider_id};route={route.content_hash}"
        ),
    )


def _cutover_readback(options: Options) -> dict:
    with SessionLocal() as session:
        snapshot = _cutover_snapshot(session, options, lock=False)
    return {**snapshot, "operation": "cutover-readback"}


def _route_snapshot(session, options: Options, *, lock: bool) -> dict:  # noqa: ANN001
    providers = _providers(session, options.provider_ids, lock=lock)
    setting = _setting(session, options.tenant_id, lock=lock)
    active = _active_route(session, options.tenant_id, options.purpose, lock=lock)
    old_route = _route_row(session, active, lock=lock) if active else None
    desired_items = [
        {"priority": priority, "provider_id": provider.id, "model_name": provider.model_name}
        for priority, provider in enumerate(providers, 1)
    ]
    body = {
        "version": SCRIPT_VERSION,
        "operation": "route",
        "tenant_id": options.tenant_id,
        "purpose": options.purpose,
        "providers": [_provider_row(provider) for provider in providers],
        "setting": _setting_row(setting),
        "old_route": old_route,
        "desired_items": desired_items,
        "desired_hash": _content_hash(desired_items),
    }
    return _with_fingerprint(body)


def _apply_route(session, options: Options, snapshot: dict) -> None:  # noqa: ANN001
    route = _replace_active_route(session, options, snapshot)
    setting = _setting(session, options.tenant_id, lock=True)
    setting.ai_provider_route_fallback_enabled = True
    setting.updated_at = _now()
    old_hash = snapshot.get("old_route") and snapshot["old_route"]["content_hash"]
    audit(
        session,
        tenant_id=options.tenant_id,
        actor=options.actor,
        action="启用AI供应商优先级降级",
        target_type="tenant_ai_provider_route_set",
        target_id=route.id,
        detail=f"{options.approval_ref};old={old_hash};new={route.content_hash}",
    )


def _replace_active_route(
    session,
    options: Options,
    snapshot: dict,
) -> TenantAiProviderRouteSet:  # noqa: ANN001
    active = _active_route(session, options.tenant_id, options.purpose, lock=True)
    if active:
        active.status = "retired"
        session.flush()
    revision = int(session.scalar(select(func.max(TenantAiProviderRouteSet.revision)).where(
        TenantAiProviderRouteSet.tenant_id == options.tenant_id,
        TenantAiProviderRouteSet.purpose == options.purpose,
    )) or 0) + 1
    route = TenantAiProviderRouteSet(
        tenant_id=options.tenant_id,
        purpose=options.purpose,
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


def _require_route_ready(snapshot: dict) -> None:
    default_provider_id = snapshot["setting"]["default_provider_id"]
    first_provider_id = snapshot["desired_items"][0]["provider_id"]
    if first_provider_id != default_provider_id:
        raise RuntimeError("provider_route_first_candidate_must_be_tenant_default")
    invalid = [
        row["id"] for row in snapshot["providers"]
        if not row["credential_enabled"]
        or not row["is_active"]
        or row["health_status"] != "健康"
    ]
    if invalid:
        raise RuntimeError(f"provider_route_not_ready:{','.join(map(str, invalid))}")


def _require_cutover_ready(snapshot: dict) -> None:
    desired_default = snapshot["desired_default_provider_id"]
    first_provider = snapshot["desired_items"][0]["provider_id"]
    if desired_default != first_provider:
        raise RuntimeError("cutover_default_must_be_first_route_candidate")
    if len({item["provider_id"] for item in snapshot["desired_items"]}) < 2:
        raise RuntimeError("cutover_requires_two_distinct_route_candidates")
    invalid = [
        row["id"] for row in snapshot["providers"]
        if not row["credential_enabled"]
        or not row["is_active"]
        or row["health_status"] != "健康"
    ]
    if invalid:
        raise RuntimeError(f"provider_route_not_ready:{','.join(map(str, invalid))}")


def _readback(options: Options) -> dict:
    with SessionLocal() as session:
        snapshot = _route_snapshot(session, options, lock=False)
    return {**snapshot, "operation": "readback"}


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


def _setting(session, tenant_id: int, *, lock: bool) -> TenantAiSetting:  # noqa: ANN001
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


def _providers_snapshot(tenant_id: int, providers: list[AiProvider]) -> dict:
    body = {
        "version": SCRIPT_VERSION,
        "operation": "providers",
        "tenant_id": tenant_id,
        "providers": [_provider_row(provider) for provider in providers],
    }
    return _with_fingerprint(body)


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


def _provider_row(provider: AiProvider) -> dict:
    return {
        "id": provider.id,
        "provider_name": provider.provider_name,
        "model_name": provider.model_name,
        "credential_enabled": provider.credential_enabled,
        "is_active": provider.is_active,
        "health_status": provider.health_status,
        "last_check_at": provider.last_check_at,
        "last_error": str(provider.last_error or "")[:240],
    }


def _setting_row(setting: TenantAiSetting) -> dict:
    return {
        "tenant_id": setting.tenant_id,
        "default_provider_id": setting.default_provider_id,
        "ai_provider_route_fallback_enabled": setting.ai_provider_route_fallback_enabled,
    }


def _content_hash(items: list[dict]) -> str:
    return hashlib.sha256(_canonical(items)).hexdigest()


def _with_fingerprint(body: dict) -> dict:
    return {**body, "fingerprint": hashlib.sha256(_canonical(body)).hexdigest()}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _require_fingerprint(snapshot: dict, expected: str) -> None:
    if snapshot["fingerprint"] != expected:
        raise RuntimeError("ai_provider_failover_fingerprint_mismatch")


if __name__ == "__main__":
    main()
