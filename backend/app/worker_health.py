from __future__ import annotations

import argparse
import logging
from datetime import timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from .config import get_settings
from .database import SessionLocal
from .timezone import beijing_now


logger = logging.getLogger(__name__)

VALID_WORKER_ROLES = {
    "all",
    "legacy",
    "planner",
    "dispatcher",
    "search-dispatcher",
    "listener",
    "recovery",
    "account-online",
    "account-security",
    "account-login",
    "ai-generation",
    "comment-generation",
    "ai-memory",
    "voice-profile",
    "material-cache",
    "metrics",
}
# `all` 健康口径：当前 mode 会启动的全部 split 专职 role 都必须新鲜。
# `task_center`/`legacy` 仅在 all-in-one worker 运行；account-login 在 mode=off 时不启动。
REQUIRED_ALL_PROCESS_TYPES = frozenset(
    {
        "planner",
        "dispatcher",
        "search-dispatcher",
        "listener",
        "recovery",
        "account-online",
        "account-security",
        "account-login",
        "ai-generation",
        "comment-generation",
        "ai-memory",
        "voice-profile",
        "material-cache",
        "metrics",
    }
)
WORKER_HEALTH_STALE_AFTER = timedelta(minutes=2)


def check_worker_health(
    *,
    role: str | None = None,
    session_factory=None,
    account_batch_login_mode: str | None = None,
) -> bool:
    """指定 role（或 `all` = 全部必需 role）是否有新鲜 heartbeat。

    `all` 要求当前 mode 的全部必需 role 新鲜；任一 role 过期即不健康，
    禁止“至少一个存活即健康”。account-login 在 mode=off 时不属于必需集合。
    """
    selected_role = _normalize_role(role)
    process_types = _health_process_types(
        selected_role,
        account_batch_login_mode=account_batch_login_mode,
    )
    fresh = fresh_worker_roles(process_types, session_factory=session_factory)
    return process_types <= fresh


def fresh_worker_roles(process_types: frozenset[str] | set[str], *, session_factory=None) -> set[str]:
    """返回给定 process_types 中 status=active 且 last_seen 未过期的 role 集合。"""
    factory = session_factory or SessionLocal
    try:
        with factory() as session:
            return set(
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
                    {
                        "cutoff": beijing_now() - WORKER_HEALTH_STALE_AFTER,
                        "process_types": tuple(process_types),
                    },
                ).scalars()
            )
    except SQLAlchemyError:
        logger.warning("worker healthcheck failed", exc_info=True)
        return set()


def stale_worker_roles(
    role: str | None = None,
    *,
    session_factory=None,
    account_batch_login_mode: str | None = None,
) -> set[str]:
    """给定 role（或 `all`）口径下缺少新鲜 heartbeat 的 role，供 stale 展示。"""
    selected_role = _normalize_role(role)
    process_types = _health_process_types(
        selected_role,
        account_batch_login_mode=account_batch_login_mode,
    )
    return set(process_types) - fresh_worker_roles(process_types, session_factory=session_factory)


def _normalize_role(role: str | None = None) -> str:
    selected_role = (role or "all").strip().lower()
    if selected_role not in VALID_WORKER_ROLES:
        raise ValueError(f"unsupported worker role: {selected_role}")
    return selected_role


def _health_process_types(
    role: str,
    *,
    account_batch_login_mode: str | None = None,
) -> set[str]:
    if role == "all":
        return set(required_all_process_types(account_batch_login_mode))
    if role == "legacy":
        return {"legacy"}
    return {role}


def required_all_process_types(
    account_batch_login_mode: str | None = None,
) -> frozenset[str]:
    mode = account_batch_login_mode
    if mode is None:
        mode = str(get_settings().account_batch_login_mode)
    required = set(REQUIRED_ALL_PROCESS_TYPES)
    if mode.strip().lower() == "off":
        required.discard("account-login")
    return frozenset(required)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TG operations worker heartbeat healthcheck")
    parser.add_argument("--role", choices=sorted(VALID_WORKER_ROLES), default=None, help="worker role to check")
    args = parser.parse_args(argv)
    healthy = check_worker_health(role=args.role)
    if not healthy:
        logger.warning(
            "worker healthcheck unhealthy role=%s stale=%s",
            args.role or "all",
            ",".join(sorted(stale_worker_roles(args.role))) or "unknown",
        )
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
