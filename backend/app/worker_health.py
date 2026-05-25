from __future__ import annotations

import argparse
import logging
from datetime import timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from .database import SessionLocal
from .timezone import beijing_now


logger = logging.getLogger(__name__)

VALID_WORKER_ROLES = {"all", "legacy", "planner", "dispatcher", "listener", "recovery", "account-security", "material-cache", "metrics"}
WORKER_HEALTH_STALE_AFTER = timedelta(minutes=2)


def check_worker_health(*, role: str | None = None) -> bool:
    selected_role = _normalize_role(role)
    process_types = _health_process_types(selected_role)
    try:
        with SessionLocal() as session:
            fresh = set(
                session.execute(
                    text(
                        """
                        SELECT DISTINCT process_type
                        FROM worker_heartbeats
                        WHERE status = 'active'
                          AND last_seen_at >= :cutoff
                          AND process_type IN :process_types
                        """
                    ).bindparams(bindparam("process_types", expanding=True)),
                    {"cutoff": beijing_now() - WORKER_HEALTH_STALE_AFTER, "process_types": tuple(process_types)},
                ).scalars()
            )
        return bool(fresh) if selected_role == "all" else process_types <= fresh
    except SQLAlchemyError:
        logger.warning("worker healthcheck failed role=%s", selected_role, exc_info=True)
        return False


def _normalize_role(role: str | None = None) -> str:
    selected_role = (role or "all").strip().lower()
    if selected_role not in VALID_WORKER_ROLES:
        raise ValueError(f"unsupported worker role: {selected_role}")
    return selected_role


def _health_process_types(role: str) -> set[str]:
    if role == "all":
        return {"task_center", "planner", "dispatcher", "listener", "recovery", "account-security", "material-cache", "metrics"}
    if role == "legacy":
        return {"legacy"}
    return {role}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TG operations worker heartbeat healthcheck")
    parser.add_argument("--role", choices=sorted(VALID_WORKER_ROLES), default=None, help="worker role to check")
    args = parser.parse_args(argv)
    return 0 if check_worker_health(role=args.role) else 1


if __name__ == "__main__":
    raise SystemExit(main())
