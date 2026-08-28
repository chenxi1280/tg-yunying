from __future__ import annotations

import re
import pytest
from app.models import TgAccount
from app.schemas.account_security import ProfileGenerationStrategy
from app.services.account_profile_name_generation import generate_username_variants
from app.services.account_security.service import _generate_profiles_from_local_pool

pytestmark = pytest.mark.no_postgres

TG_USERNAME_RE = re.compile(r"^[a-z][a-z0-9_]{4,31}$")


def test_generate_username_variants_from_real_username():
    variants = generate_username_variants(raw_username="gang2639", display_name="雾影", seed=42, max_candidates=5)
    assert len(variants) > 0
    for v in variants:
        assert TG_USERNAME_RE.match(v), f"Invalid Telegram username format: {v}"
        assert not v.endswith("_")
        assert "__" not in v


def test_generate_username_variants_from_display_name_fallback():
    variants = generate_username_variants(raw_username="", display_name="WillWang", seed=10, max_candidates=5)
    assert len(variants) > 0
    for v in variants:
        assert TG_USERNAME_RE.match(v), f"Invalid Telegram username format: {v}"


def test_local_profile_generation_defaults_to_blank_bio():
    accounts = [
        TgAccount(id=1, username="alex99", display_name="老王", phone_masked="138****0001"),
        TgAccount(id=2, username="cat_lover", display_name="小李", phone_masked="138****0002"),
    ]
    strategy = ProfileGenerationStrategy(
        generation_mode="local_random",
        bio_enabled=True,
        username_enabled=True,
        username_max_attempts=3,
    )
    results = _generate_profiles_from_local_pool(accounts, strategy, seed="test-seed")
    assert len(results) == 2
    for r in results:
        assert r["bio"] == ""
        assert len(r["username_candidates"]) == 3
        assert all(not username.startswith("tg_user_") for username in r["username_candidates"])
        for u in r["username_candidates"]:
            assert TG_USERNAME_RE.match(u)
