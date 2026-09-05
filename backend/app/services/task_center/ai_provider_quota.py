from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.ai_transport_errors import AiProviderResultUnknown
from app.models import AiProvider, AiProviderHealthStatus
from app.services._common import _now


AI_PROVIDER_QUOTA_EXHAUSTED_MARKERS = (
    "quota exhausted",
    "insufficient quota",
    "insufficient balance",
    "quota_exhausted",
    "余额不足",
    "配额不足",
    "配额耗尽",
    "用量上限",
    "token plan",
    "购买积分补充用量",
    "token_limit_exceeded",
    "antigravity_quota_limited",
)


def is_ai_provider_quota_exhausted(error: Exception) -> bool:
    detail = str(error).lower()
    return any(marker in detail for marker in AI_PROVIDER_QUOTA_EXHAUSTED_MARKERS)


def mark_provider_quota_exhausted(provider: AiProvider, error: Exception) -> None:
    provider.health_status = AiProviderHealthStatus.UNHEALTHY.value
    provider.last_check_at = _now()
    provider.last_error = f"AI provider quota exhausted: {str(error)[:300]}"
    provider.updated_at = _now()


def observe_provider_quota_failure(
    session: Session, provider: AiProvider, error: Exception, *, commit: bool,
) -> bool:
    if not is_ai_provider_quota_exhausted(error):
        return False
    try:
        mark_provider_quota_exhausted(provider, error)
        if commit:
            session.add(provider)
            session.commit()
    except SQLAlchemyError as persistence_error:
        if isinstance(error, AiProviderResultUnknown):
            raise error from persistence_error
        raise
    return True
