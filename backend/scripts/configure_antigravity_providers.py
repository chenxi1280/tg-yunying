from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import dataclass

from sqlalchemy import or_, select, text

from app.database import SessionLocal
from app.models import AiProvider, AiProviderHealthStatus
from app.security import decrypt_secret, encrypt_secret
from app.services._common import _now, audit
from app.services.antigravity_provider_identity import canonical_antigravity_base_url


PROVIDER_TYPE = "antigravity_cli"
BASE_URL = "http://host.docker.internal:18101"
PROVIDERS = (
    ("Antigravity slot-01 3.6 Flash Medium", "gemini-3.6-flash-medium"),
    ("Antigravity slot-01 3.1 Pro Low", "gemini-3.1-pro-low"),
)


@dataclass(frozen=True)
class Options:
    operation: str
    tenant_id: int
    expected_fingerprint: str
    actor: str
    approval_ref: str
    deployed_sha: str


def main() -> None:
    options = _options()
    token = os.environ.get("ANTIGRAVITY_BRIDGE_TOKEN", "")
    if not token:
        raise RuntimeError("ANTIGRAVITY_BRIDGE_TOKEN is required")
    if options.operation != "apply":
        snapshot = _snapshot(options, token=token, lock=False)
        result = {**snapshot, "operation": options.operation}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
        return
    applied = _apply(options, token)
    result = _snapshot(options, token=token, lock=False)
    print(json.dumps({"applied": applied, "noop": not applied, "readback": result}, ensure_ascii=False, sort_keys=True, default=str))


def _options() -> Options:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("preview", "apply", "readback"))
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="codex-production-remediation")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--deployed-sha", required=True)
    args = parser.parse_args()
    if args.operation == "apply" and (
        len(args.expected_fingerprint) != 64 or not args.approval_ref
    ):
        parser.error("apply requires fingerprint and approval ref")
    if len(args.deployed_sha) != 40 or any(
        character not in "0123456789abcdefABCDEF" for character in args.deployed_sha
    ):
        parser.error("full --deployed-sha is required")
    return Options(
        args.operation, args.tenant_id, args.expected_fingerprint,
        args.actor, args.approval_ref, args.deployed_sha,
    )


def _snapshot(options: Options, *, token: str, lock: bool) -> dict:
    runtime_sha = _require_runtime_sha(options)
    with SessionLocal() as session:
        statement = select(AiProvider).where(
            or_(
                AiProvider.provider_name.in_([name for name, _model in PROVIDERS]),
                AiProvider.provider_type == PROVIDER_TYPE,
            )
        )
        if lock:
            statement = statement.with_for_update()
        by_name = _unique_provider_rows(list(session.scalars(statement)))
        return _snapshot_from_rows(options, by_name, token)


def _snapshot_from_rows(
    options: Options,
    by_name: dict[str, AiProvider],
    token: str,
) -> dict:
    rows = [_provider_row(by_name.get(name), name, model) for name, model in PROVIDERS]
    body = {
        "version": "antigravity_provider_config_v1",
        "tenant_id": options.tenant_id,
        "deployed_sha": options.deployed_sha,
        "runtime_sha": os.environ.get("RELEASE_SHA", ""),
        "desired_base_url": BASE_URL,
        "desired_token_fingerprint": _token_fingerprint(token),
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
        "token_fingerprint": _stored_token_fingerprint(row),
    }


def _apply(options: Options, token: str) -> bool:
    _require_runtime_sha(options)
    with SessionLocal() as session:
        _advisory_lock(session)
        rows = list(session.scalars(
            select(AiProvider)
            .where(or_(
                AiProvider.provider_name.in_([name for name, _model in PROVIDERS]),
                AiProvider.provider_type == PROVIDER_TYPE,
            ))
            .with_for_update()
        ))
        by_name = _unique_provider_rows(rows)
        _require_fingerprint(
            _snapshot_from_rows(options, by_name, token), options.expected_fingerprint,
        )
        changed = False
        for name, model in PROVIDERS:
            provider = by_name.get(name)
            if provider is not None and _provider_matches(provider, token, model):
                continue
            if provider is None:
                provider = AiProvider(provider_name=name, api_key_ciphertext="")
                session.add(provider)
            _configure_provider(provider, token, model)
            changed = True
            session.flush()
            audit(
                session, tenant_id=options.tenant_id, actor=options.actor,
                action="配置Antigravity AI供应商", target_type="ai_provider",
                target_id=str(provider.id), detail=f"{options.approval_ref};model={model}",
            )
        session.commit()
    return changed


def _require_runtime_sha(options: Options) -> str:
    runtime_sha = os.environ.get("RELEASE_SHA", "")
    if runtime_sha != options.deployed_sha:
        raise RuntimeError("antigravity_provider_runtime_sha_mismatch")
    return runtime_sha


def _unique_provider_rows(rows: list[AiProvider]) -> dict[str, AiProvider]:
    by_name: dict[str, AiProvider] = {}
    duplicates: set[str] = set()
    for row in rows:
        if row.provider_name in by_name:
            duplicates.add(row.provider_name)
        by_name[row.provider_name] = row
    if duplicates:
        raise RuntimeError(
            "antigravity_provider_duplicate:" + ",".join(sorted(duplicates))
        )
    identities: dict[tuple[str, str, str], int] = {}
    for row in rows:
        if row.provider_type != PROVIDER_TYPE:
            continue
        identity = (
            row.provider_type,
            canonical_antigravity_base_url(str(row.base_url or "")),
            str(row.model_name or "").strip().lower(),
        )
        if identity in identities:
            raise RuntimeError(
                f"antigravity_provider_identity_duplicate:{identities[identity]},{row.id}"
            )
        identities[identity] = row.id
    return by_name


def _advisory_lock(session) -> None:  # noqa: ANN001
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 36101})


def _provider_matches(provider: AiProvider, token: str, model: str) -> bool:
    return (
        provider.provider_type == PROVIDER_TYPE
        and provider.base_url == BASE_URL
        and provider.model_name == model
        and provider.api_key_header == "Authorization"
        and not provider.is_billable
        and provider.credential_enabled
        and provider.is_active
        and decrypt_secret(provider.api_key_ciphertext) == token
    )


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


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _stored_token_fingerprint(provider: AiProvider) -> str:
    try:
        return _token_fingerprint(decrypt_secret(provider.api_key_ciphertext))
    except Exception:  # noqa: BLE001 - only the fingerprint is exposed to operators.
        return "unreadable"


if __name__ == "__main__":
    main()
