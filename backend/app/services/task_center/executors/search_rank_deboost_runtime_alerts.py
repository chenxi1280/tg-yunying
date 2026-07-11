from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import Action, TgAccount
from app.models.search_rank_deboost import AccountGroupProxyBinding
from app.services.search_rank_deboost_alerts import record_group_proxy_egress_failure_alert


def record_proxy_egress_alert(
    session: Session,
    *,
    action: Action,
    account: TgAccount,
    binding_id: int,
    probe_exit_ip: str | None,
) -> None:
    binding = session.get(AccountGroupProxyBinding, int(binding_id))
    binding_active = binding is not None and binding.status == "active"
    observed_exit_ip = (binding.observed_exit_ip or "") if binding else ""
    record_group_proxy_egress_failure_alert(
        session,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(account.id),
        binding_id=int(binding_id),
        binding_active=binding_active,
        observed_exit_ip=observed_exit_ip,
        probe_exit_ip=(probe_exit_ip or "").strip(),
    )


__all__ = ["record_proxy_egress_alert"]
