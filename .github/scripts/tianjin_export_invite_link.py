from __future__ import annotations

import json

from app.database import SessionLocal
from app.models import OperationTarget, Tenant, TgAccount
from app.models.enums import AccountStatus
from app.services._common import _now
from app.services.developer_apps import credentials_for_account
from app.services.operations import audit
from app.services.task_center.dispatcher import gateway


TENANT_ID = 1
TARGET_ID = 485


def main() -> None:
    with SessionLocal() as session:
        tenant = session.get(Tenant, TENANT_ID)
        target = session.get(OperationTarget, TARGET_ID)
        if not tenant or not target or target.tenant_id != TENANT_ID:
            raise RuntimeError("天津运营目标或租户不存在")
        admin = session.get(TgAccount, tenant.group_rescue_admin_account_id or 0)
        if not admin or admin.status != AccountStatus.ACTIVE.value or not admin.session_ciphertext:
            raise RuntimeError("天津救援管理员账号不可用，无法导出邀请链接")
        result = gateway.export_group_invite_link(
            admin.id,
            target.tg_peer_id,
            admin.session_ciphertext,
            credentials_for_account(session, admin),
        )
        if not result.ok or not result.invite_link:
            raise RuntimeError(f"天津邀请链接导出失败: {result.failure_type or result.detail}")
        invite_link = result.invite_link.strip()
        target.username = invite_link
        target.updated_at = _now()
        audit(
            session,
            tenant_id=TENANT_ID,
            actor="github-actions",
            action="导出天津运营目标邀请链接",
            target_type="operation_target",
            target_id=str(target.id),
            detail=f"exporter_account_id={admin.id}",
        )
        session.commit()
        print(
            "TIANJIN_INVITE_LINK_EXPORT="
            + json.dumps(
                {
                    "exporter_account_id": admin.id,
                    "invite_link_prefix": invite_link[:18],
                    "invite_link_length": len(invite_link),
                    "target_id": target.id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
