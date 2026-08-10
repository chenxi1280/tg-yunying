from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from sqlalchemy import delete, func, select

from app.database import Base, SessionLocal, engine
from app.integrations.telegram import GroupMessageSnapshot
from app.models import (
    AccountStatus,
    ConversationSpeakerState,
    ConversationSpeakerTurn,
    GroupContextMessage,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services import group_listeners


TENANT_ID = 983
GROUP_ID = 983
LISTENER_IDS = (9831, 9832)


def test_second_listener_fetch_holds_no_speaker_write_lock(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    _seed()
    second_fetch_started = Event()
    release_fetch = Event()
    monkeypatch.setattr(group_listeners, "credentials_for_account", lambda *_args: object())
    monkeypatch.setattr(group_listeners, "_listener_context_account_error", lambda _account: "")

    def fetch(_session, *, account, **_kwargs):
        if account.id == LISTENER_IDS[1]:
            second_fetch_started.set()
            assert release_fetch.wait(timeout=5)
        return [[_snapshot(account.id)]]

    monkeypatch.setattr(group_listeners, "fetch_listener_snapshot_pages", fetch)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_collect_and_commit)
            assert second_fetch_started.wait(timeout=5)
            with SessionLocal() as session:
                state = session.scalar(
                    select(ConversationSpeakerState)
                    .where(ConversationSpeakerState.tenant_id == TENANT_ID)
                    .with_for_update(nowait=True)
                )
                assert state is not None
                session.rollback()
            release_fetch.set()
            assert future.result(timeout=5) == 2
        with SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(GroupContextMessage).where(
                GroupContextMessage.tenant_id == TENANT_ID,
            )) == 2
    finally:
        release_fetch.set()
        _cleanup()


def _collect_and_commit() -> int:
    with SessionLocal() as session:
        group = session.get(TgGroup, GROUP_ID)
        inserted = group_listeners.collect_group_context(session, group)
        session.commit()
        return inserted


def _snapshot(account_id: int) -> GroupMessageSnapshot:
    return GroupMessageSnapshot(
        remote_message_id=str(account_id),
        sender_peer_id=f"human-{account_id}",
        sender_name="真人用户",
        content=f"消息-{account_id}",
    )


def _seed() -> None:
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="listener two phase"))
        session.add(TgGroup(
            id=GROUP_ID,
            tenant_id=TENANT_ID,
            tg_peer_id=f"-100{GROUP_ID}",
            title="listener two phase",
            listener_enabled=True,
        ))
        session.add(ConversationSpeakerState(
            tenant_id=TENANT_ID,
            surface="group_ai_chat",
            conversation_key=f"group:{GROUP_ID}",
        ))
        for account_id in LISTENER_IDS:
            session.add(TgAccount(
                id=account_id,
                tenant_id=TENANT_ID,
                phone_masked=f"+{account_id}",
                display_name=f"listener-{account_id}",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext=f"session-{account_id}",
            ))
            session.add(TgGroupAccount(
                tenant_id=TENANT_ID,
                group_id=GROUP_ID,
                account_id=account_id,
                is_listener=True,
            ))
        session.commit()


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(GroupContextMessage).where(
            GroupContextMessage.tenant_id == TENANT_ID,
        ))
        session.execute(delete(ConversationSpeakerTurn).where(
            ConversationSpeakerTurn.tenant_id == TENANT_ID,
        ))
        session.execute(delete(ConversationSpeakerState).where(
            ConversationSpeakerState.tenant_id == TENANT_ID,
        ))
        session.execute(delete(TgGroupAccount).where(TgGroupAccount.tenant_id == TENANT_ID))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id == TENANT_ID))
        session.execute(delete(TgGroup).where(TgGroup.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
