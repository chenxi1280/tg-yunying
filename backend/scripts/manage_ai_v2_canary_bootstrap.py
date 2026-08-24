from __future__ import annotations

import argparse
import base64
import json

from app.database import SessionLocal
from app.services.task_center.ai_v2_canary_bootstrap import (
    apply_bootstrap,
    parse_choices,
    preview_bootstrap,
    readback_bootstrap,
)


def main() -> None:
    options = _options()
    choices = parse_choices(_decode_choices(options.choices_b64))
    if options.operation == "preview":
        result = _preview(options.tenant_id, choices)
    elif options.operation == "apply":
        result = _apply(options.tenant_id, choices, options.expected_fingerprint)
    else:
        result = _readback(options.tenant_id, choices.task_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))


def _options():  # noqa: ANN202
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("preview", "apply", "readback"))
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--choices-b64", default="")
    parser.add_argument("--expected-fingerprint", default="")
    args = parser.parse_args()
    if args.operation == "apply" and len(args.expected_fingerprint) != 64:
        parser.error("apply requires a 64-character --expected-fingerprint")
    if args.operation == "readback" and not args.choices_b64:
        parser.error("readback requires choices with task_id")
    return args


def _decode_choices(value: str) -> dict:
    if not value:
        return {}
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(f"{value}{padding}")
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("ai_v2_bootstrap_choices_invalid") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ai_v2_bootstrap_choices_invalid")
    return payload


def _preview(tenant_id: int, choices) -> dict:  # noqa: ANN001
    with SessionLocal() as session:
        return preview_bootstrap(session, tenant_id, choices)


def _apply(tenant_id: int, choices, fingerprint: str) -> dict:  # noqa: ANN001
    with SessionLocal() as session:
        result = apply_bootstrap(
            session,
            tenant_id,
            choices,
            expected_fingerprint=fingerprint,
        )
        session.commit()
    with SessionLocal() as session:
        readback = readback_bootstrap(session, tenant_id, choices.task_id)
    return {**result, "readback": readback}


def _readback(tenant_id: int, task_id: str) -> dict:
    with SessionLocal() as session:
        return readback_bootstrap(session, tenant_id, task_id)


if __name__ == "__main__":
    main()
