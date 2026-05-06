"""Base schema types used across all domain modules."""
from __future__ import annotations


from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ListQuery(BaseModel):
    tenant_id: int = 1
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)
    search: str | None = None
    status: str | None = None


__all__ = ["ApiModel", "ListQuery"]
