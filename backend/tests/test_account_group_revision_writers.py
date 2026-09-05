from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import AccountGroupMembershipRevision, StageWakeOutbox, TgAccount
from app.schemas import AccountPoolCreate, TgAccountCreate
from app.security import encrypt_secret
from app.services.account_group_revision_snapshot import current_group_revisions
from app.services.account_login.binding import bind_or_create_account
from app.services.account_pools import (
    backfill_ungrouped_accounts_to_default_pool, create_account_pool, list_account_pools,
    move_account_pool, set_account_identity,
)
from app.services.accounts import create_account, soft_delete_account
from tests.test_account_phone_alias_backfill import session_factory


pytestmark = pytest.mark.no_postgres


def _create(session):
    return create_account(session, TgAccountCreate(tenant_id=1, pool_id=10,
        display_name="版本测试账号", phone_number="+12025550123"), "tester")


def _current(session, pool_id=10):
    return current_group_revisions(session, 1, (pool_id,))[0]


def test_normal_create_move_and_soft_delete_append_exact_membership_successors(session_factory):
    with session_factory() as session:
        account = _create(session)
        original = _current(session)
        assert original.versions == (2, 1)
        assert original.membership.member_account_ids == [account.id]
        target = create_account_pool(session, AccountPoolCreate(tenant_id=1, name="目标组"), "tester")
        move_account_pool(session, account.id, target["id"], "tester")
        moved = _current(session, target["id"])
        assert _current(session).versions == (3, 1)
        assert _current(session).membership.member_account_ids == []
        assert moved.versions == (2, 1) and moved.membership.member_account_ids == [account.id]
        soft_delete_account(session, account.id, "tester", "测试移除")
        deleted = _current(session, target["id"])
        assert deleted.versions == (3, 1) and deleted.membership.member_account_ids == []
        assert original.membership.member_account_ids == moved.membership.member_account_ids == [account.id]


def test_identity_change_records_both_groups_and_frozen_member_purpose(session_factory):
    with session_factory() as session:
        account = _create(session)
        original = _current(session)
        changed = set_account_identity(session, account.id, "code_receiver", "tester")
        target = _current(session, changed.pool_id)
        assert _current(session).membership.member_account_ids == []
        assert target.membership.member_contracts[0]["account_identity"] == "code_receiver"
        assert original.membership.member_contracts[0]["account_identity"] == "normal"


def test_batch_login_create_and_existing_phone_reentry_do_not_duplicate_revisions(session_factory):
    item = SimpleNamespace(id=1, tenant_id=1, line_no=1, phone_masked="test",
        phone_ciphertext=encrypt_secret("+12025550123"), account_id=None)
    with session_factory() as session:
        first = bind_or_create_account(session, item, 10, "tester")
        initial = _current(session)
        session.commit()
        second = bind_or_create_account(session, item, 10, "tester")
        session.commit()
        assert first.created and not second.created
        assert initial.versions == (2, 1)
        assert _current(session).membership.id == initial.membership.id
        assert _current(session).membership.member_account_ids == [first.account.id]
        assert session.scalar(select(func.count(StageWakeOutbox.id))) == 3


def test_pool_list_does_not_backfill_but_explicit_backfill_records_delta(session_factory):
    with session_factory() as session:
        account = TgAccount(tenant_id=1, pool_id=None, display_name="旧未分组", phone_masked="legacy")
        session.add(account)
        session.commit()
        list_account_pools(session, 1)
        assert account.pool_id is None
        assert session.scalar(select(func.count(AccountGroupMembershipRevision.id))) == 0
        assert backfill_ungrouped_accounts_to_default_pool(session, 1) == 1
        session.commit()
        pair = _current(session)
        assert pair.versions == (2, 1) and pair.membership.member_account_ids == [account.id]
