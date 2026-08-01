from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import pytest

from app.database import Base
from app.integrations.telegram import GroupMessageSnapshot
from app.models import (
    Action,
    ExecutionAttempt,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center.remote_history_evidence import (
    preview_remote_history_evidence,
)
from app.services.task_center.remote_reconciliation import (
    ensure_remote_reconcile_case,
)


pytestmark = pytest.mark.no_postgres


def test_unique_same_account_text_history_confirms_without_mutation() -> None:
    engine = _engine()
    with Session(engine) as session:
        case, observed_at = _seed_case(session)
        gateway = _gateway([
            _snapshot("remote-1", observed_at, sender="viewer-1"),
        ])

        evidence = preview_remote_history_evidence(
            session,
            case.id,
            gateway_client=gateway,
            credentials_resolver=lambda *_args: object(),
        )

        assert evidence.result == "remote_confirmed"
        assert evidence.remote_message_id == "remote-1"
        assert evidence.exact_match_count == 1
        assert gateway.calls == 1


def test_ambiguous_or_missing_history_never_proves_absence() -> None:
    engine = _engine()
    with Session(engine) as session:
        case, observed_at = _seed_case(session)
        gateway = _gateway([
            _snapshot("remote-1", observed_at, sender="viewer-1"),
            _snapshot("remote-2", observed_at, sender="viewer-1"),
        ])

        evidence = preview_remote_history_evidence(
            session,
            case.id,
            gateway_client=gateway,
            credentials_resolver=lambda *_args: object(),
        )

        assert evidence.result == "inconclusive"
        assert evidence.exact_match_count == 2
        gateway.snapshots = []
        missing = preview_remote_history_evidence(
            session,
            case.id,
            gateway_client=gateway,
            credentials_resolver=lambda *_args: object(),
        )
        assert missing.result == "inconclusive"


def test_other_sender_or_media_action_cannot_be_auto_confirmed() -> None:
    engine = _engine()
    with Session(engine) as session:
        case, observed_at = _seed_case(session)
        gateway = _gateway([
            _snapshot("remote-1", observed_at, sender="someone-else"),
        ])
        evidence = preview_remote_history_evidence(
            session,
            case.id,
            gateway_client=gateway,
            credentials_resolver=lambda *_args: object(),
        )
        assert evidence.result == "inconclusive"

        action = session.get(Action, case.action_id)
        action.payload = {**action.payload, "media_segments": [{"type": "image"}]}
        unsupported = preview_remote_history_evidence(
            session,
            case.id,
            gateway_client=_gateway([]),
            credentials_resolver=lambda *_args: object(),
        )
        assert unsupported.result == "inconclusive"
        assert unsupported.source == "telegram_history_unsupported_action"


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_case(session: Session):
    observed_at = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task-1",
        tenant_id=1,
        name="remote",
        type="group_ai_chat",
        status="running",
    ))
    session.add(TgGroup(
        id=8,
        tenant_id=1,
        tg_peer_id="-1008",
        title="group",
    ))
    session.add(TgAccount(
        id=11,
        tenant_id=1,
        display_name="account",
        phone_masked="***11",
        status="在线",
        session_ciphertext="session",
    ))
    action = Action(
        id="action-1",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        scheduled_at=observed_at - timedelta(minutes=1),
        status="unknown_after_send",
        payload={
            "group_id": 8,
            "chat_id": "-1008",
            "message_text": "exact body",
        },
        result={"gateway_request_identity": "request-1"},
    )
    attempt = ExecutionAttempt(
        id="attempt-1",
        tenant_id=1,
        action_id=action.id,
        account_id=11,
        attempt_no=1,
        status="result_unknown",
        before_call_at=observed_at - timedelta(seconds=2),
        gateway_call_started_at=observed_at - timedelta(seconds=1),
        after_call_at=observed_at,
        result_snapshot={"gateway_request_identity": "request-1"},
    )
    session.add_all([action, attempt])
    session.flush()
    return ensure_remote_reconcile_case(session, action, attempt), observed_at


def _snapshot(remote_id: str, sent_at, *, sender: str) -> GroupMessageSnapshot:
    return GroupMessageSnapshot(
        remote_message_id=remote_id,
        sender_name="sender",
        content="exact body",
        viewer_peer_id="viewer-1",
        sender_peer_id=sender,
        sent_at=sent_at,
    )


def _gateway(snapshots):
    gateway = SimpleNamespace(snapshots=list(snapshots), calls=0)

    def fetch(*_args, **_kwargs):
        gateway.calls += 1
        return list(gateway.snapshots)

    gateway.fetch_group_messages = fetch
    return gateway
