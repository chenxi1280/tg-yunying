from __future__ import annotations

import argparse
from dataclasses import dataclass

from app.services.task_center.ai_provider_routes import ANTIGRAVITY_GENERATION_PURPOSES


DEFAULT_ROUTE_PURPOSE = "group_realize_general"
GENERATION_OPERATIONS = frozenset({
    "generation-cutover-preview",
    "generation-cutover-apply",
    "generation-readback",
})


@dataclass(frozen=True)
class Options:
    operation: str
    tenant_id: int
    provider_ids: tuple[int, ...]
    expected_fingerprint: str
    actor: str
    approval_ref: str
    purpose: str = DEFAULT_ROUTE_PURPOSE
    deployed_sha: str = ""


def parse_options() -> Options:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=(
        "providers-preview", "providers-apply", "provider-check",
        "default-preview", "default-apply",
        "route-preview", "route-apply", "readback",
        "cutover-preview", "cutover-apply",
        "generation-cutover-preview", "generation-cutover-apply",
        "generation-readback",
    ))
    parser.add_argument("--tenant-id", type=int, required=True)
    parser.add_argument("--provider-id", type=int, action="append", required=True)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", default="codex-production-remediation")
    parser.add_argument("--approval-ref", default="")
    parser.add_argument("--purpose", default=DEFAULT_ROUTE_PURPOSE)
    parser.add_argument("--deployed-sha", required=True)
    args = parser.parse_args()
    provider_ids = tuple(dict.fromkeys(args.provider_id))
    _validate_args(parser, args, provider_ids)
    return Options(
        args.operation, args.tenant_id, provider_ids,
        args.expected_fingerprint, args.actor, args.approval_ref,
        args.purpose, args.deployed_sha,
    )


def _validate_args(parser, args, provider_ids: tuple[int, ...]) -> None:  # noqa: ANN001
    if len(provider_ids) < 2:
        parser.error("at least two distinct --provider-id values are required")
    if args.operation.endswith("apply") and (
        len(args.expected_fingerprint) != 64 or not args.approval_ref
    ):
        parser.error("apply requires --expected-fingerprint and --approval-ref")
    if args.operation == "provider-check" and not args.approval_ref:
        parser.error("provider-check requires --approval-ref")
    if args.operation in GENERATION_OPERATIONS and not args.approval_ref:
        parser.error("generation operations require --approval-ref")
    if len(args.deployed_sha) != 40 or any(
        character not in "0123456789abcdefABCDEF" for character in args.deployed_sha
    ):
        parser.error("full deployed SHA is required")
    if args.operation in GENERATION_OPERATIONS and len(provider_ids) != 2:
        parser.error("generation operations require exactly two providers")
    if args.purpose not in ANTIGRAVITY_GENERATION_PURPOSES:
        parser.error("purpose is not an approved Antigravity generation route")


__all__ = [
    "DEFAULT_ROUTE_PURPOSE",
    "GENERATION_OPERATIONS",
    "Options",
    "parse_options",
]
