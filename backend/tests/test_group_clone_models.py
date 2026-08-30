from __future__ import annotations

from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.no_postgres

from app.database import Base
from app.models import Tenant, TgAccount, TgAccountAuthorization, Task
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateEvent,
    TelegramAuthorizationUpdateSubscription,
    TelegramAuthorizationUpdateDelivery,
    TelegramOutboundRandomIdMapping,
)
from app.models.telegram_authorities import (
    TelegramGroupMutationAuthority,
    TelegramGroupMutationAuthorityHolder,
    TelegramAuthorizationTransportState,
)
from app.models.group_clone import (
    CloneSourceStreamState,
    CloneSourceEvent,
    CloneTargetRouteSnapshot,
    CloneTargetExecutionSnapshot,
    CloneAccountSlot,
    CloneSenderBindingHistory,
    CloneAlbumManifest,
    CloneAlbumItem,
    CloneTopicMap,
    TelegramGatewayMutationIdentity,
    CloneDeliveryObligation,
    CloneMessagePart,
    CloneManualReviewDecision,
    CloneSequencerHeadCase,
    CloneCutoverExclusion,
)


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    # Seed baseline tenant and accounts
    tenant = Tenant(id=1, name="Default Tenant")
    session.add(tenant)
    session.flush()

    account = TgAccount(id=101, tenant_id=1, display_name="Acc 101", phone_masked="+1234567890", status="online")
    session.add(account)
    session.flush()

    auth = TgAccountAuthorization(
        id=201,
        tenant_id=1,
        account_id=101,
        session_ciphertext="enc_test",
        is_current=True,
        slot_generation=1,
    )
    session.add(auth)
    session.flush()

    task = Task(
        id="task-clone-100",
        tenant_id=1,
        name="Test Clone Task",
        type="group_clone",
        status="running",
        fulfillment_contract_version="v2_group_clone",
    )
    session.add(task)
    session.flush()

    yield session
    session.close()


def test_telegram_authorization_update_state_lifecycle(db_session: Session):
    state = TelegramAuthorizationUpdateState(
        tenant_id=1,
        account_id=101,
        authorization_id=201,
        session_generation=1,
        common_pts=100,
        common_qts=50,
        common_seq=1,
        state="live",
    )
    db_session.add(state)
    db_session.commit()

    loaded = db_session.get(TelegramAuthorizationUpdateState, state.id)
    assert loaded is not None
    assert loaded.common_pts == 100
    assert loaded.state == "live"


def test_telegram_group_mutation_authority_exclusive(db_session: Session):
    auth = TelegramGroupMutationAuthority(
        tenant_id=1,
        target_peer_type="channel",
        target_peer_id="-100123456",
        mode="exclusive_clone",
        gateway_admission_side="new",
        state="active",
    )
    db_session.add(auth)
    db_session.flush()

    holder = TelegramGroupMutationAuthorityHolder(
        authority_id=auth.id,
        writer_kind="group_clone",
        writer_id="task-clone-100",
        route_hash="hash_test",
        holder_role="primary",
        state="active",
    )
    db_session.add(holder)
    db_session.commit()

    loaded_auth = db_session.get(TelegramGroupMutationAuthority, auth.id)
    assert loaded_auth is not None
    assert loaded_auth.mode == "exclusive_clone"


def test_clone_source_event_and_obligation_chain(db_session: Session):
    now_utc = datetime.now(timezone.utc)
    ev = CloneSourceEvent(
        tenant_id=1,
        task_id="task-clone-100",
        task_lifecycle_epoch=1,
        source_peer_type="channel",
        source_peer_id="-100987654",
        source_message_id=501,
        event_type="message_new",
        event_identity_hash="ev_hash_501",
        apply_order_key="501",
        stream_order_no=1,
        sender_peer_type="user",
        sender_peer_id="user_123",
        content="Hello world",
        content_fingerprint="content_hash_1",
    )
    db_session.add(ev)
    db_session.flush()

    obl = CloneDeliveryObligation(
        tenant_id=1,
        task_id="task-clone-100",
        epoch=1,
        source_event_id=ev.id,
        obligation_kind="send",
        stream_order_no=1,
        sequencer_id=1,
        planned_at=now_utc,
        state="ready",
    )
    db_session.add(obl)
    db_session.flush()

    head_case = CloneSequencerHeadCase(
        task_id="task-clone-100",
        epoch=1,
        sequencer_id=1,
        obligation_id=obl.id,
        case_kind="failed_terminal",
        policy_snapshot="fail_stop",
        state="waiting_decision",
    )
    db_session.add(head_case)
    db_session.commit()

    loaded_case = db_session.get(CloneSequencerHeadCase, head_case.id)
    assert loaded_case is not None
    assert loaded_case.state == "waiting_decision"
