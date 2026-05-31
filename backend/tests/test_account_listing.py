from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models import AccountStatus, TgAccount
from test_workflow import SessionLocal, auth_headers


def test_tg_accounts_returns_pagination_headers_for_full_list_loading():
    with TestClient(app) as client:
        headers = auth_headers(client)
        with SessionLocal() as session:
            for index in range(55):
                session.add(
                    TgAccount(
                        tenant_id=1,
                        display_name=f"分页账号 {index:02d}",
                        username=f"page_account_{index:02d}",
                        phone_masked=f"+86135555{index:04d}",
                        status=AccountStatus.ACTIVE.value,
                        health_score=90,
                    )
                )
            session.commit()

        response = client.get("/api/tg-accounts?page=1&page_size=20", headers=headers)

        assert response.status_code == 200
        assert len(response.json()) == 20
        assert int(response.headers["X-Total-Count"]) >= 55
        assert response.headers["X-Page"] == "1"
        assert response.headers["X-Page-Size"] == "20"
