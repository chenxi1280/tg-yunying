from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


# 降权任务 6 类风控告警类别（spec 定义）
RANK_DEBOOST_ALERT_GROUP_IP_DRIFT = "rank_deboost_group_ip_drift"
RANK_DEBOOST_ALERT_NODE_UNREACHABLE = "rank_deboost_node_unreachable"
RANK_DEBOOST_ALERT_JOIN_BUTTON_VIOLATION = "rank_deboost_join_button_violation"
RANK_DEBOOST_ALERT_ACCOUNT_ISOLATION_VIOLATION = "rank_deboost_account_isolation_violation"
RANK_DEBOOST_ALERT_EXEMPT_GROUP_MISSING = "rank_deboost_exempt_group_missing"
RANK_DEBOOST_ALERT_ALL_EXEMPT_CLICKS = "rank_deboost_all_exempt_clicks"

RANK_DEBOOST_ALERT_TYPES = frozenset({
    RANK_DEBOOST_ALERT_GROUP_IP_DRIFT,
    RANK_DEBOOST_ALERT_NODE_UNREACHABLE,
    RANK_DEBOOST_ALERT_JOIN_BUTTON_VIOLATION,
    RANK_DEBOOST_ALERT_ACCOUNT_ISOLATION_VIOLATION,
    RANK_DEBOOST_ALERT_EXEMPT_GROUP_MISSING,
    RANK_DEBOOST_ALERT_ALL_EXEMPT_CLICKS,
})


class SearchRankDeboostAlert(Base):
    """降权任务风控告警记录。

    与 ProxyAlert（本地代理资源告警）解耦：降权告警围绕分组级代理绑定、
    账号组隔离、豁免群、按钮自检等降权专属场景，不一定关联 AccountProxy，
    因此独立建表，通过 alert_type 区分 6 类场景。
    """

    __tablename__ = "search_rank_deboost_alerts"
    __table_args__ = (
        Index(
            "ix_search_rank_deboost_alert_tenant_type_status",
            "tenant_id",
            "alert_type",
            "status",
        ),
        Index(
            "ix_search_rank_deboost_alert_task",
            "tenant_id",
            "task_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    alert_type: Mapped[str] = mapped_column(String(60), default="")
    severity: Mapped[str] = mapped_column(String(20), default="warning")
    task_id: Mapped[str] = mapped_column(String(36), default="")
    action_id: Mapped[str] = mapped_column(String(36), default="")
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    reason_code: Mapped[str] = mapped_column(String(80), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="alerting")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = [
    "RANK_DEBOOST_ALERT_ACCOUNT_ISOLATION_VIOLATION",
    "RANK_DEBOOST_ALERT_ALL_EXEMPT_CLICKS",
    "RANK_DEBOOST_ALERT_EXEMPT_GROUP_MISSING",
    "RANK_DEBOOST_ALERT_GROUP_IP_DRIFT",
    "RANK_DEBOOST_ALERT_JOIN_BUTTON_VIOLATION",
    "RANK_DEBOOST_ALERT_NODE_UNREACHABLE",
    "RANK_DEBOOST_ALERT_TYPES",
    "SearchRankDeboostAlert",
]
