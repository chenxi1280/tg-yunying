from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant
from app.services.notifications import NotificationResult, notify_ai_failure


@pytest.mark.no_postgres
def test_ai_failure_notification_sends_to_all_admin_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sent: list[tuple[str, str, str]] = []

    def fake_send(bot_token: str, chat_id: str, text: str) -> NotificationResult:
        sent.append((bot_token, chat_id, text))
        return NotificationResult(True, f"ok:{chat_id}")

    monkeypatch.setattr("app.services.notifications.decrypt_secret", lambda value: value)
    monkeypatch.setattr("app.services.notifications.send_telegram_bot_message", fake_send)

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                notify_ai_failures_enabled=True,
                admin_chat_id="1001\n1002, 1003",
                telegram_bot_token_ciphertext="bot-token",
            )
        )
        session.commit()

        result = notify_ai_failure(
            session,
            tenant_id=1,
            title="AI 运营任务失败",
            detail="provider down",
            target_type="operation_task",
            target_id="42",
        )

    assert result.ok is True
    assert [chat_id for _, chat_id, _ in sent] == ["1001", "1002", "1003"]
