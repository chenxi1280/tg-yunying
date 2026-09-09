"""Screen incoming group snapshots before they can affect any business state."""

from app.content_safety import SCREENING_VERSION, content_fingerprint, content_screening_reason
from app.services._common import audit


def reject_unsafe_snapshot(session, group, snapshot) -> bool:
    content = str(getattr(snapshot, "content", "") or "")
    reason = content_screening_reason(content)
    if not reason:
        return False
    source_id = str(getattr(snapshot, "remote_message_id", "") or "")
    audit(session, tenant_id=group.tenant_id, actor="content-screening",
          action="屏蔽性交易广告消息", target_type="tg_group", target_id=str(group.id),
          detail=f"reason={reason}; source_id={source_id}; hash={content_fingerprint(content)}; version={SCREENING_VERSION}")
    return True
