from __future__ import annotations

import argparse
import hashlib
import json

from app.database import SessionLocal
from app.services.authorization_dr import apply_runtime_configuration, preview_runtime_configuration


def main() -> None:
    args = _parser().parse_args()
    desired = {
        "mode": args.runtime_mode,
        "app_a_id": args.app_a_id,
        "app_b_id": args.app_b_id,
        "app_c_id": args.app_c_id,
        "egress_id": args.egress_id,
        "egress_secret_ref_digest": hashlib.sha256(args.egress_secret_ref.encode()).hexdigest(),
        "observed_ip_hmac": hashlib.sha256(args.expected_egress_ip.encode()).hexdigest(),
    }
    with SessionLocal() as session:
        preview = preview_runtime_configuration(session, desired)
        result = preview if args.mode == "preview" else apply_runtime_configuration(
            session,
            desired,
            expected_fingerprint=args.expected_fingerprint,
            actor=args.actor,
        )
    print("AUTHORIZATION_DR_RUNTIME=" + json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("preview", "apply"), required=True)
    parser.add_argument("--runtime-mode", choices=("off", "shadow", "migrate"), required=True)
    parser.add_argument("--app-a-id", type=int, required=True)
    parser.add_argument("--app-b-id", type=int, required=True)
    parser.add_argument("--app-c-id", type=int, required=True)
    parser.add_argument("--egress-id", required=True)
    parser.add_argument("--egress-secret-ref", required=True)
    parser.add_argument("--expected-egress-ip", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="")
    return parser


if __name__ == "__main__":
    main()
