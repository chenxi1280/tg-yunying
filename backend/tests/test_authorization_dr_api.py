from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.routers import authorization_dr
from app.database import get_session


pytestmark = pytest.mark.no_postgres


def test_claim_without_available_operation_returns_empty_204(monkeypatch) -> None:
    api = FastAPI()
    api.include_router(authorization_dr.router)
    api.dependency_overrides[authorization_dr._internal_node_identity] = lambda: "my-node-1"
    api.dependency_overrides[get_session] = lambda: object()
    monkeypatch.setattr(authorization_dr, "claim_migration_operation", lambda session, node_id: None)

    with TestClient(api) as client:
        response = client.post(
            "/internal/v1/authorization-dr/operations/claim",
            json={"purpose": "migrate_standby_2"},
        )

    assert response.status_code == 204
    assert response.content == b""


def test_phone_banned_endpoint_records_authoritative_failure(monkeypatch) -> None:
    api = FastAPI()
    api.include_router(authorization_dr.router)
    api.dependency_overrides[authorization_dr._internal_node_identity] = lambda: "my-node-1"
    api.dependency_overrides[get_session] = lambda: object()
    recorded = {}

    def mark_failed(session, operation_id, **fields):
        recorded.update({"operation_id": operation_id, **fields})

    monkeypatch.setattr(authorization_dr, "mark_login_remote_failed", mark_failed)

    with TestClient(api) as client:
        response = client.post(
            "/internal/v1/authorization-dr/operations/operation-1/login-failed",
            json={
                "owner_epoch": 2,
                "lease_token": "lease-token",
                "blocker_code": "phone_number_banned",
            },
        )

    assert response.status_code == 204
    assert response.content == b""
    assert recorded == {
        "operation_id": "operation-1",
        "node_id": "my-node-1",
        "owner_epoch": 2,
        "lease_token": "lease-token",
        "blocker_code": "phone_number_banned",
    }
