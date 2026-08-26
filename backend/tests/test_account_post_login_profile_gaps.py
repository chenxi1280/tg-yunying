from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.integrations.telegram.contracts import RemoteAvatarFingerprint, RemoteProfile
from app.models import (
    TgAccount,
    TgAccountProfileNameClaim,
    TgAccountSecurityBatch,
)
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.contracts import FullInitializationClaim
from app.services.account_post_login_init.profile import execute_profile_stage
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres


class _ProfileGateway:
    def __init__(self, name: str, avatar_hash: str) -> None:
        self.name = name
        self.avatar_hash = avatar_hash

    def pull_profile(self, *_args):
        return RemoteProfile(first_name=self.name, last_name="", bio="")

    def pull_profile_avatar_fingerprint(self, *_args, **_kwargs):
        return RemoteAvatarFingerprint(
            sha256="remote-sha",
            size_bytes=100,
            remote_photo_id="remote-photo",
            perceptual_hash=self.avatar_hash,
        )


def _current_gap_owner(session, key: str):
    _, first_item = _new_login_item(session, f"{key}-first")
    predecessor = create_or_attach_full_initialization(session, first_item, actor="操作员")
    predecessor.status = predecessor.stage = "succeeded"
    predecessor.two_fa_status = predecessor.profile_status = predecessor.abc_status = "succeeded"
    predecessor.profile_target_name = "林岚"
    predecessor.profile_target_avatar_source = "material:9"
    predecessor.profile_target_avatar_object_key = "avatars/linlan.jpg"
    _, current_item = _new_login_item(session, f"{key}-current")
    owner = create_or_attach_full_initialization(session, current_item, actor="操作员")
    account = session.get(TgAccount, 40)
    account.display_name = account.tg_first_name = "林岚"
    account.tg_last_name = ""
    account.avatar_object_key = "avatars/linlan.jpg"
    session.add(TgAccountProfileNameClaim(
        tenant_id=1,
        account_id=40,
        display_name="林岚",
        name_key="林岚",
        batch_id=None,
        batch_item_id=None,
    ))
    owner.status = "running"
    owner.stage = "profile"
    owner.two_fa_status = "succeeded"
    owner.lease_token = f"{key}-lease"
    session.commit()
    return owner


def test_current_matching_profile_performs_zero_mutation(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import profile

    monkeypatch.setattr(profile, "gateway", _ProfileGateway("林岚", "0" * 16))
    monkeypatch.setattr(
        profile,
        "_local_avatar_fingerprint",
        lambda _key: {"sha256": "local-sha", "perceptual_hash": "0" * 16},
    )
    with session_factory() as session:
        owner = _current_gap_owner(session, "profile-zero-gap")
        claim = FullInitializationClaim(owner.id, owner.stage, owner.lease_token)

    execute_profile_stage(session_factory, claim)

    with session_factory() as session:
        owner = session.get(type(owner), owner.id)
        batches = list(session.scalars(select(TgAccountSecurityBatch)))

    assert batches == []
    assert owner.profile_status == "succeeded"
    assert owner.stage == "abc"


@pytest.mark.parametrize(
    "case",
    [
        ("林岚", "f" * 16, ["update_avatar"]),
        ("不同姓名", "0" * 16, ["update_profile"]),
    ],
)
def test_profile_gap_records_only_missing_action(session_factory, monkeypatch, case) -> None:
    from app.services.account_post_login_init import profile

    remote_name, remote_avatar_hash, expected = case
    captured: list[str] = []
    monkeypatch.setattr(profile, "gateway", _ProfileGateway(remote_name, remote_avatar_hash))
    monkeypatch.setattr(
        profile,
        "_local_avatar_fingerprint",
        lambda _key: {"sha256": "local-sha", "perceptual_hash": "0" * 16},
    )
    monkeypatch.setattr(
        profile,
        "_create_profile_batch",
        lambda _factory, _claim, owner: captured.extend(json.loads(owner.profile_action_types)),
    )
    with session_factory() as session:
        owner = _current_gap_owner(session, f"profile-gap-{expected[0]}")
        claim = FullInitializationClaim(owner.id, owner.stage, owner.lease_token)

    execute_profile_stage(session_factory, claim)

    assert captured == expected
