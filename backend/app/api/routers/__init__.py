from __future__ import annotations
from fastapi import APIRouter

from .accounts import router as accounts_router
from .account_pools import router as account_pools_router
from .ai_config import router as ai_config_router
from .archives import router as archives_router
from .audit import router as audit_router
from .auth import router as auth_router
from .campaigns import router as campaigns_router
from .developer_apps import router as developer_apps_router
from .groups import router as groups_router
from .system import router as system_router


router = APIRouter()
for sub_router in (
    system_router,
    auth_router,
    developer_apps_router,
    ai_config_router,
    account_pools_router,
    accounts_router,
    groups_router,
    campaigns_router,
    archives_router,
    audit_router,
):
    router.include_router(sub_router)


__all__ = ["router"]
