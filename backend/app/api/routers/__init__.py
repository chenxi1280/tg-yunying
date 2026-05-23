from __future__ import annotations
from fastapi import APIRouter

from app.config import get_settings

from .accounts import router as accounts_router
from .account_security import router as account_security_router
from .account_pools import router as account_pools_router
from .ai_config import router as ai_config_router
from .archives import router as archives_router
from .audit import router as audit_router
from .auth import router as auth_router
from .developer_apps import router as developer_apps_router
from .groups import router as groups_router
from .message_tasks import router as message_tasks_router
from .operations import router as operations_router
from .operation_plans import router as operation_plans_router
from .operations_center import router as operations_center_router
from .risk_control import router as risk_control_router
from .system import router as system_router
from .task_center import router as task_center_router


router = APIRouter()
for sub_router in (
    system_router,
    auth_router,
    developer_apps_router,
    ai_config_router,
    account_pools_router,
    accounts_router,
    account_security_router,
    operations_router,
    operation_plans_router,
    operations_center_router,
    risk_control_router,
    groups_router,
    message_tasks_router,
    task_center_router,
    archives_router,
    audit_router,
):
    router.include_router(sub_router)

if get_settings().enable_legacy_campaign_routes:
    from .campaigns import router as campaigns_router

    router.include_router(campaigns_router)


__all__ = ["router"]
