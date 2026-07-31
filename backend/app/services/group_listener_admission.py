"""Bridge listener polling to the group-bot admission observation state machine."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.models import TgAccount, TgGroup

from ._common import gateway
from .group_listener_cursor import listener_after_message_id


class ListenerSnapshotFetchError(RuntimeError):
    def __init__(self, account_id: int, detail: str) -> None:
        super().__init__(detail)
        self.account_id = account_id


def fetch_listener_snapshots(session: Session, *, group: TgGroup, account: TgAccount, credentials) -> list[object]:
    try:
        after_message_id = listener_after_message_id(group)
        cursor_args = {"after_message_id": after_message_id} if after_message_id is not None else {}
        return list(
            gateway.fetch_group_messages(
                account.id,
                group.tg_peer_id,
                account.session_ciphertext,
                credentials,
                limit=group.listener_context_limit,
                **cursor_args,
            )
        )
    except Exception as exc:  # noqa: BLE001 - persist explicit observation failure before listener worker exposes it.
        record_group_bot_observations(session, group=group, account=account, snapshots=(), failure_code="listener_fetch_failed")
        raise ListenerSnapshotFetchError(account.id, str(exc)) from exc


def record_group_bot_observations(
    session: Session,
    *,
    group: TgGroup,
    account: TgAccount,
    snapshots: Iterable[object],
    failure_code: str = "",
) -> None:
    from .task_center.group_bot_admission import close_observation_if_due
    from .task_center.group_bot_observation import observing_admissions, record_listener_observations

    record_listener_observations(
        session,
        group=group,
        listener_account_id=account.id,
        snapshots=snapshots,
        failure_code=failure_code,
    )
    for admission in observing_admissions(session, group=group):
        close_observation_if_due(session, admission=admission)


__all__ = ["ListenerSnapshotFetchError", "fetch_listener_snapshots", "record_group_bot_observations"]
