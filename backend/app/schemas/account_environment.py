from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProxyAirportSubscriptionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subscription_url: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def normalize_url(self) -> "ProxyAirportSubscriptionUpdate":
        self.subscription_url = self.subscription_url.strip()
        if not self.subscription_url.startswith(("http://", "https://")):
            raise ValueError("Clash 订阅地址必须是 http 或 https")
        return self


class ProxyAirportSubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int | None = None
    tenant_id: int
    subscription_url_configured: bool
    subscription_url_preview: str
    provider_type: str = "clash"
    sync_status: str
    node_count: int
    healthy_node_count: int
    last_sync_at: datetime | None = None
    last_error: str = ""
    updated_at: datetime | None = None


class AccountEnvironmentBindingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    developer_app_id: int
    authorization_id: int
    session_role: str = Field(pattern="^(primary|standby_1|standby_2)$")
    proxy_id: int | None = None
    device_model: str = Field(min_length=1, max_length=120)
    system_version: str = Field(min_length=1, max_length=80)
    app_version: str = Field(min_length=1, max_length=60)
    platform: str = Field(min_length=1, max_length=40)
    lang_code: str = Field(default="zh", max_length=16)
    system_lang_code: str = Field(default="zh-CN", max_length=16)
    lang_pack: str = Field(default="", max_length=40)
    region_code: str = Field(default="CN", max_length=16)
    client_identity_key: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def normalize_fields(self) -> "AccountEnvironmentBindingPatch":
        for field in ["device_model", "system_version", "app_version", "platform", "client_identity_key"]:
            value = str(getattr(self, field) or "").strip()
            if not value:
                raise ValueError(f"{field} 不能为空")
            setattr(self, field, value)
        return self


class AccountEnvironmentBindingOut(BaseModel):
    id: str | None = None
    account_id: int
    account_display_name: str
    account_username: str
    phone_masked: str
    account_status: str
    developer_app_id: int | None = None
    developer_app_name: str
    developer_app_api_id_snapshot: int
    authorization_id: int | None = None
    session_role: str
    authorization_status: str
    proxy_id: int | None = None
    proxy_name: str
    proxy_status: str
    device_model: str
    system_version: str
    app_version: str
    platform: str
    observed_device_model: str = ""
    observed_system_version: str = ""
    observed_app_version: str = ""
    observed_api_id: int = 0
    observed_missing_fields: list[str] = []
    lang_code: str
    system_lang_code: str
    lang_pack: str
    region_code: str
    client_identity_key: str
    consistency_status: str
    effect_boundary: str
    updated_at: datetime | None = None
