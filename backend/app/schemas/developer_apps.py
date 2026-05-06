from __future__ import annotations
from datetime import datetime

from pydantic import BaseModel, Field

from .api import ApiModel


class DeveloperAppCreate(BaseModel):
    app_name: str
    api_id: int = Field(..., ge=1)
    api_hash: str = Field(..., min_length=8)
    is_active: bool = True
    max_accounts: int = Field(default=0, ge=0)
    notes: str = ""


class DeveloperAppUpdate(BaseModel):
    app_name: str | None = None
    api_hash: str | None = Field(default=None, min_length=8)
    is_active: bool | None = None
    max_accounts: int | None = Field(default=None, ge=0)
    notes: str | None = None


class DeveloperAppOut(ApiModel):
    id: int
    app_name: str
    api_id: int
    is_active: bool
    health_status: str
    max_accounts: int
    assigned_accounts: int = 0
    credentials_version: int
    last_assigned_at: datetime | None  # noqa: F821
    last_check_at: datetime | None  # noqa: F821
    last_error: str
    notes: str
    created_at: datetime  # noqa: F821
    updated_at: datetime  # noqa: F821


__all__ = ["DeveloperAppCreate", "DeveloperAppOut", "DeveloperAppUpdate"]
