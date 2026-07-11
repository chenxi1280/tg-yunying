from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def new_uuid() -> str:
    return str(uuid4())


class SearchRankDeboostExemptGroup(Base):
    """任务级预选随机豁免群。任务创建时从当时搜索结果中随机选 1 个非我方目标群。"""

    __tablename__ = "search_rank_deboost_exempt_groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", name="uq_search_rank_deboost_exempt_group_task"),
        Index("ix_search_rank_deboost_exempt_group_task", "tenant_id", "task_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(String(36))
    exempt_group_username: Mapped[str] = mapped_column(String(120), default="")
    exempt_group_peer_id: Mapped[str] = mapped_column(String(64), default="")
    exempt_group_title: Mapped[str] = mapped_column(String(255), default="")
    exempt_group_match_strategy: Mapped[str] = mapped_column(String(40), default="username")
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    selected_by: Mapped[str] = mapped_column(String(100), default="")
    selection_audit_id: Mapped[str] = mapped_column(String(80), default="")
    previous_exempt_group_username: Mapped[str] = mapped_column(String(120), default="")
    previous_exempt_group_peer_id: Mapped[str] = mapped_column(String(64), default="")


class SearchRankDeboostActionStat(Base):
    """降权 action 统计：每次点击竞争群写一条记录。"""

    __tablename__ = "search_rank_deboost_action_stats"
    __table_args__ = (
        Index("ix_search_rank_deboost_stat_task_time", "tenant_id", "task_id", "captured_at"),
        Index("ix_search_rank_deboost_stat_account_hour", "tenant_id", "account_id", "hour_bucket"),
        Index("ix_search_rank_deboost_stat_group_hour", "tenant_id", "account_pool_id", "hour_bucket"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(String(36))
    action_id: Mapped[str] = mapped_column(String(36), default="")
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    account_pool_id: Mapped[int] = mapped_column(ForeignKey("account_pools.id"))
    proxy_airport_node_id: Mapped[int | None] = mapped_column(ForeignKey("proxy_airport_nodes.id"), nullable=True)
    observed_exit_ip: Mapped[str] = mapped_column(String(64), default="")
    bot_username: Mapped[str] = mapped_column(String(80), default="jisou")
    keyword_hash: Mapped[str] = mapped_column(String(64), default="")
    competitor_group_username: Mapped[str] = mapped_column(String(120), default="")
    competitor_group_peer_id: Mapped[str] = mapped_column(String(64), default="")
    competitor_group_title: Mapped[str] = mapped_column(String(255), default="")
    competitor_position: Mapped[int] = mapped_column(Integer, default=0)
    button_hash: Mapped[str] = mapped_column(String(120), default="")
    button_effect: Mapped[str] = mapped_column(String(40), default="navigate_only")
    join_button_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    joined: Mapped[bool] = mapped_column(Boolean, default=False)
    dwell_seconds: Mapped[int] = mapped_column(Integer, default=0)
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    skip_reason: Mapped[str] = mapped_column(String(80), default="")
    join_button_violation: Mapped[bool] = mapped_column(Boolean, default=False)


class SearchRankDeboostClickReservation(Base):
    __tablename__ = "search_rank_deboost_click_reservations"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_rank_deboost_reservation_action"),
        Index("ix_rank_deboost_reservation_account_date_status", "tenant_id", "account_id", "local_date", "status"),
        Index(
            "ix_rank_deboost_reservation_account_keyword_date_status",
            "tenant_id",
            "account_id",
            "keyword_hash",
            "local_date",
            "status",
        ),
        Index("ix_rank_deboost_reservation_pool_date_status", "tenant_id", "account_pool_id", "local_date", "status"),
        Index("ix_rank_deboost_reservation_task_hour_status", "task_id", "hour_bucket", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    action_id: Mapped[str] = mapped_column(ForeignKey("actions.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    account_pool_id: Mapped[int] = mapped_column(ForeignKey("account_pools.id"))
    keyword_hash: Mapped[str] = mapped_column(String(64))
    local_date: Mapped[date] = mapped_column(Date)
    hour_bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reserved_count: Mapped[int] = mapped_column(Integer, default=1)
    consumed_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="reserved")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountGroupProxyBinding(Base):
    """分组级代理绑定：1 分组 = 1 代理节点，组内多账号共享出口 IP。"""

    __tablename__ = "account_group_proxy_bindings"
    __table_args__ = (
        Index(
            "uq_account_group_proxy_binding_active_pool",
            "tenant_id",
            "account_pool_id",
            "status",
            unique=True,
            sqlite_where=text("status = 'active' AND unbound_at IS NULL"),
            postgresql_where=text("status = 'active' AND unbound_at IS NULL"),
        ),
        Index(
            "uq_account_group_proxy_binding_active_node",
            "tenant_id",
            "proxy_airport_node_id",
            "status",
            unique=True,
            sqlite_where=text("status = 'active' AND unbound_at IS NULL"),
            postgresql_where=text("status = 'active' AND unbound_at IS NULL"),
        ),
        Index("ix_account_group_proxy_binding_node", "tenant_id", "proxy_airport_node_id", "status"),
        Index("ix_account_group_proxy_binding_pool", "tenant_id", "account_pool_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    account_pool_id: Mapped[int] = mapped_column(ForeignKey("account_pools.id"))
    proxy_airport_node_id: Mapped[int] = mapped_column(ForeignKey("proxy_airport_nodes.id"))
    runtime_proxy_id: Mapped[int | None] = mapped_column(
        ForeignKey("account_proxies.id", name="fk_account_group_binding_runtime_proxy"),
        nullable=True,
    )
    binding_scope: Mapped[str] = mapped_column(String(24), default="group")
    observed_exit_ip: Mapped[str] = mapped_column(String(64), default="")
    observed_exit_country: Mapped[str] = mapped_column(String(16), default="")
    observed_exit_asn: Mapped[str] = mapped_column(String(80), default="")
    observed_exit_isp: Mapped[str] = mapped_column(String(120), default="")
    exit_ip_stability_score: Mapped[float] = mapped_column(Float, default=100.0)
    health_score: Mapped[float] = mapped_column(Float, default=100.0)
    last_failover_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    binding_generation: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="active")
    change_reason: Mapped[str] = mapped_column(String(255), default="")
    bound_by: Mapped[str] = mapped_column(String(100), default="")
    bound_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    unbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_probe_error: Mapped[str] = mapped_column(String(255), default="")


__all__ = [
    "SearchRankDeboostExemptGroup",
    "SearchRankDeboostActionStat",
    "SearchRankDeboostClickReservation",
    "AccountGroupProxyBinding",
]
