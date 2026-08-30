from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass

from sqlalchemy import select, text

from app.database import SessionLocal
from app.models import AiProvider, AiProviderHealthStatus
from app.security import encrypt_secret
from app.services._common import _now, audit


PROVIDER_TYPE = "antigravity_cli"
BASE_URL = "http://host.docker.internal:18101"
PROVIDERS = (
    ("Antigravity slot-01 3.5 Flash Medium", "gemini-3.5-flash-medium"),
    ("Antigravity slot-01 3.1 Pro Low", "gemini-3.1-pro-low"),
)


@dataclass(frozen=True)
class Options:
    operation: str
    tenant_id: int
    expected_fingerprint: str
    actor: str
    approval_ref: str


def main() -> None:
    options = _options()
    if options.operation != "apply":
        snapshot = _snapshot(options, lock=False)
        result = {**snapshot, "operation": options.operation}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return
    token = os.environ.get("ANTIGRAVITY_BRIDGE_TOKEN", "")
    if not token:
        raise RuntimeError("ANTIGRAVITY_BRIDGE_TOKEN is required")
    _apply(options, token)
    result = _snapshot(options, lock=False)
    print(json.dumps({"applied": True, "readback": result}, ensure_ascii=False, sort_keys=True, default=str))


def _options() -> Options:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("preview", "apply", "readback"))
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="codex-production-remediation")
    parser.add_argument("--approval-ref", default="")
    args = parser.parse_args()
    if args.operation == "apply" and (
        len(args.expected_fingerprint) != 64 or not args.approval_ref
    ):
        parser.error("apply requires fingerprint and approval ref")
    return Options(
        args.operation, args.tenant_id, args.expected_fingerprint,
        args.actor, args.approval_ref,
    )


def _snapshot(options: Options, *, lock: bool) -> dict:
    with SessionLocal() as session:
        statement = select(AiProvider).where(
            AiProvider.provider_name.in_([name for name, _model in PROVIDERS])
        )
        if lock:
            statement = statement.with_for_update()
        by_name = {row.provider_name: row for row in session.scalars(statement)}
        return _snapshot_from_rows(options, by_name)


def _snapshot_from_rows(options: Options, by_name: dict[str, AiProvider]) -> dict:
    rows = [_provider_row(by_name.get(name), name, model) for name, model in PROVIDERS]
    body = {
        "version": "antigravity_provider_config_v1",
        "tenant_id": options.tenant_id,
        "desired_base_url": BASE_URL,
        "providers": rows,
    }
    return {**body, "fingerprint": _fingerprint(body)}


def _provider_row(row: AiProvider | None, name: str, model: str) -> dict:
    if row is None:
        return {
            "id": None, "provider_name": name, "provider_type": PROVIDER_TYPE,
            "base_url": BASE_URL, "model_name": model, "health_status": "missing",
        }
    return {
        "id": row.id, "provider_name": row.provider_name,
        "provider_type": row.provider_type, "base_url": row.base_url,
        "model_name": row.model_name, "credential_enabled": row.credential_enabled,
        "is_active": row.is_active, "health_status": row.health_status,
    }


def _apply(options: Options, token: str) -> None:
    with SessionLocal() as session:
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 36101})
        rows = list(session.scalars(
            select(AiProvider)
            .where(AiProvider.provider_name.in_([name for name, _model in PROVIDERS]))
            .with_for_update()
        ))
        by_name = {row.provider_name: row for row in rows}
        _require_fingerprint(
            _snapshot_from_rows(options, by_name), options.expected_fingerprint,
        )
        for name, model in PROVIDERS:
            provider = by_name.get(name)
            if provider is None:
                provider = AiProvider(provider_name=name, api_key_ciphertext="")
                session.add(provider)
            _configure_provider(provider, token, model)
            session.flush()
            audit(
                session, tenant_id=options.tenant_id, actor=options.actor,
                action="配置Antigravity AI供应商", target_type="ai_provider",
                target_id=str(provider.id), detail=f"{options.approval_ref};model={model}",
            )
        session.commit()


def _configure_provider(provider: AiProvider, token: str, model: str) -> None:
    provider.provider_type = PROVIDER_TYPE
    provider.base_url = BASE_URL
    provider.model_name = model
    provider.api_key_ciphertext = encrypt_secret(token)
    provider.api_key_header = "Authorization"
    provider.is_billable = False
    provider.credential_enabled = True
    provider.is_active = True
    provider.health_status = AiProviderHealthStatus.UNHEALTHY.value
    provider.last_error = "provider identity changed; run check before routing"
    provider.notes = "Antigravity CLI slot-01; isolated host bridge"
    provider.updated_at = _now()


def _require_fingerprint(snapshot: dict, expected: str) -> None:
    if snapshot["fingerprint"] != expected:
        raise RuntimeError("preview fingerprint drifted")


def _fingerprint(body: dict) -> str:
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    main()
