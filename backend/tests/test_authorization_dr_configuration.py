from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import pytest

from app.database import Base
from app.models import TelegramDeveloperApp
from app.services.authorization_dr import apply_runtime_configuration, preview_runtime_configuration


pytestmark = pytest.mark.no_postgres


def test_runtime_configuration_uses_exact_three_apps_and_shadow_first() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    desired = {
        "mode": "shadow",
        "app_a_id": 1,
        "app_b_id": 2,
        "app_c_id": 3,
        "egress_id": "my-egress-1",
        "egress_secret_ref_digest": "a" * 64,
        "observed_ip_hmac": "b" * 64,
    }
    with Session(engine) as session:
        session.add_all([
            TelegramDeveloperApp(id=1, app_name="A", api_id=101, api_hash_ciphertext="a"),
            TelegramDeveloperApp(id=2, app_name="B", api_id=102, api_hash_ciphertext="b"),
            TelegramDeveloperApp(id=3, app_name="C", api_id=103, api_hash_ciphertext="c"),
        ])
        session.commit()
        preview = preview_runtime_configuration(session, desired)

        result = apply_runtime_configuration(
            session,
            desired,
            expected_fingerprint=preview["configuration_fingerprint"],
            actor="operator",
        )

        assert result["mode"] == "shadow"
        assert [(row["purpose"], row["app_id"]) for row in result["assignments"]] == [
            ("primary_sv", 1),
            ("standby_1_sv", 2),
            ("standby_2_my", 3),
        ]
        assert result["egress"][0]["connectivity"] == "verified"
