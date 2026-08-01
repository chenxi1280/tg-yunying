from __future__ import annotations

from datetime import timedelta
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import OperationResult
from app.models import (
    Action,
    ExecutionAttempt,
    GatewayRequestEvidenceJournal,
    GroupBotRequiredChannelFollow,
    OperationTarget,
    RemoteReconcileCase,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center import dispatcher, service
from app.services.task_center.gateway_evidence_journal import (
    bind_gateway_request_identity,
)
from app.services.task_center.group_bot_admission import (
    ensure_admission_after_join,
    ingest_trusted_bot_prompt,
)
from app.services.task_center.payloads import (
    GroupBotConfirmationButtonPayload,
    GroupBotRequiredChannelFollowPayload,
)
from app.services.task_center.recovery_claims import RecoveryClaim
from app.services.task_center.remote_reconciliation import (
    RemoteReconcileEvidence,
    apply_remote_reconcile_evidence,
    ensure_remote_reconcile_case,
    typed_remote_fact_id,
)


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_follow(session: Session):
    account = TgAccount(id=11, tenant_id=1, display_name="a", phone_masked="11")
    task = Task(id="task-ai", tenant_id=1, name="ai", type="group_ai_chat", status="running")
    session.add_all([Tenant(id=1, name="t"), account, task])
    admission = ensure_admission_after_join(
        session, tenant_id=1, group_id=7, account_id=11,
        membership_action_id="join-1", join_start_cursor="100",
    )
    ingest_trusted_bot_prompt(
        session,
        admission=admission,
        message_id="bot-1",
        text="请关注 @school_news",
        bot_peer_id="900",
        is_admin_bot=True,
        control_buttons=[{
            "row": 0, "col": 0, "text": "关注频道",
            "url": "https://t.me/school_news", "action_type": "url",
        }],
        bound_task_id=task.id,
    )
    action = session.scalar(select(Action).where(Action.action_type == "group_bot_channel_follow"))
    assert action is not None
    return account, admission, action


def _seed_confirmation(session: Session):
    group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="g", group_type="supergroup")
    account = TgAccount(id=11, tenant_id=1, display_name="a", phone_masked="11")
    task = Task(id="task-ai", tenant_id=1, name="ai", type="group_ai_chat", status="running")
    session.add_all([Tenant(id=1, name="t"), group, account, task])
    admission = ensure_admission_after_join(
        session, tenant_id=1, group_id=7, account_id=11,
        membership_action_id="join-1", join_start_cursor="100",
    )
    admission.state = "awaiting_group_bot_confirmation"
    admission.trusted_bot_peer_id = "900"
    admission.source_message_id = "bot-1"
    payload = GroupBotConfirmationButtonPayload(
        group_id=7,
        admission_id=admission.id,
        admission_version=admission.admission_version,
        source_message_id="bot-1",
        trusted_bot_peer_id="900",
        button_row=0,
        button_col=0,
        button_text="我已加入",
        admission_bound_task_id=task.id,
        admission_bound_account_id=account.id,
    )
    action = Action(
        id="confirm-1", tenant_id=1, task_id=task.id,
        task_type=task.type, action_type="group_bot_confirmation_button",
        account_id=account.id, status="executing", payload=payload.model_dump(),
    )
    session.add(action)
    return group, account, admission, payload, action


def test_group_bot_follow_persists_b0_and_result_journal(monkeypatch) -> None:
    with _session() as session:
        account, _admission, action = _seed_follow(session)
        payload = GroupBotRequiredChannelFollowPayload.model_validate(action.payload)
        monkeypatch.setattr(
            dispatcher.gateway,
            "follow_group_bot_required_channel",
            lambda *_args: OperationResult(True, detail="followed", remote_mutation_started=True),
        )

        dispatcher._dispatch_group_bot_required_channel_follow(
            session, action, account, credentials=None, payload=payload,
        )

        attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id))
        journal = session.scalar(select(GatewayRequestEvidenceJournal).where(GatewayRequestEvidenceJournal.action_id == action.id))
        assert attempt is not None and attempt.gateway_call_started_at is not None
        assert attempt.status == "success"
        assert journal is not None and journal.remote_fact_id


def test_group_bot_callback_persists_b0_and_result_journal(monkeypatch) -> None:
    with _session() as session:
        group, account, admission, payload, action = _seed_confirmation(session)
        monkeypatch.setattr(dispatcher, "refresh_live_confirmation_source", lambda _context: payload)
        monkeypatch.setattr(
            dispatcher.gateway,
            "click_group_bot_confirmation_button",
            lambda *_args: OperationResult(True, detail="clicked", remote_mutation_started=True),
        )

        dispatcher._click_refreshed_group_bot_confirmation(
            session, action=action, admission=admission, group=group,
            account=account, credentials=None, payload=payload,
        )

        attempt = session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id))
        journal = session.scalar(select(GatewayRequestEvidenceJournal).where(GatewayRequestEvidenceJournal.action_id == action.id))
        assert attempt is not None and attempt.gateway_call_started_at is not None
        assert attempt.status == "success"
        assert journal is not None and journal.remote_fact_id


def test_group_bot_follow_remote_confirmed_replays_admission_fact() -> None:
    with _session() as session:
        account, admission, action = _seed_follow(session)
        attempt = _unknown_attempt(session, action, account)
        case = ensure_remote_reconcile_case(session, action, attempt)
        fact_id = typed_remote_fact_id(action, attempt, "group_bot_channel_follow")

        apply_remote_reconcile_evidence(
            session,
            case.id,
            RemoteReconcileEvidence(
                result="remote_confirmed",
                source="gateway_request_evidence_journal",
                evidence_fingerprint="a" * 64,
                remote_fact_id=fact_id,
                remote_mutation_started=True,
                exact_match_count=1,
            ),
            actor="qa",
        )

        follow = session.scalar(select(GroupBotRequiredChannelFollow).where(GroupBotRequiredChannelFollow.admission_id == admission.id))
        assert follow is not None and follow.status == "success"
        assert action.status == "success"


def test_group_bot_callback_remote_confirmed_replays_click_fact() -> None:
    with _session() as session:
        _group, account, admission, _payload, action = _seed_confirmation(session)
        attempt = _unknown_attempt(session, action, account)
        case = ensure_remote_reconcile_case(session, action, attempt)
        fact_id = typed_remote_fact_id(
            action,
            attempt,
            "group_bot_confirmation_button",
        )

        apply_remote_reconcile_evidence(
            session,
            case.id,
            RemoteReconcileEvidence(
                result="remote_confirmed",
                source="gateway_request_evidence_journal",
                evidence_fingerprint="b" * 64,
                remote_fact_id=fact_id,
                remote_mutation_started=True,
                exact_match_count=1,
            ),
            actor="qa",
        )

        assert action.status == "success"
        assert action.result["confirmation_click"] == "accepted_waiting_bot_confirmation"
        assert admission.state == "awaiting_group_bot_confirmation"


def _unknown_attempt(session: Session, action: Action, account: TgAccount) -> ExecutionAttempt:
    attempt = ExecutionAttempt(
        tenant_id=action.tenant_id, action_id=action.id, worker_id="worker",
        account_id=account.id, attempt_no=1, status="result_unknown",
        before_call_at=_now(), gateway_call_started_at=_now(), result_snapshot={},
    )
    session.add(attempt)
    session.flush()
    bind_gateway_request_identity(action, attempt)
    action.status = "unknown_after_send"
    session.flush()
    return attempt


def test_membership_reprobe_closes_the_existing_remote_case(monkeypatch) -> None:
    with _session() as session:
        action, attempt, task = _seed_unknown_membership(session)
        case = ensure_remote_reconcile_case(session, action, attempt)
        session.commit()
        claim = RecoveryClaim(action.id, "recovery-token")
        action.claim_owner = "recovery:test"
        action.claim_token = claim.token
        action.claim_expires_at = _now() + timedelta(minutes=5)
        session.commit()
        monkeypatch.setattr(
            service.gateway,
            "probe_target_capabilities",
            lambda *_args, **_kwargs: OperationResult(True, detail="membership observed"),
        )
        monkeypatch.setattr(
            service,
            "credentials_for_account",
            lambda *_args: None,
        )

        assert service._recover_unknown_membership_action(
            session, action=action, task=task, latest_attempt=attempt,
            now=_now(), recovery_claim=claim,
        ) is True

        session.flush()
        assert case.state == "remote_confirmed"
        assert action.status == "success"
        assert action.result["remote_fact_id"]


def test_membership_inconclusive_case_can_retry_with_a_new_exact_claim(monkeypatch) -> None:
    with _session() as session:
        action, attempt, task = _seed_unknown_membership(session)
        case = ensure_remote_reconcile_case(session, action, attempt)
        session.commit()
        first_claim = _set_recovery_claim(action, "first-token")
        session.commit()
        calls = iter((TimeoutError("probe timeout"), OperationResult(True, detail="joined")))

        def probe(*_args, **_kwargs):
            result = next(calls)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(service.gateway, "probe_target_capabilities", probe)
        monkeypatch.setattr(service, "credentials_for_account", lambda *_args: None)

        assert service._recover_claimed_unknown_action(
            session,
            first_claim,
            now=_now(),
            reprobed_identities=set(),
        ) == 0
        assert case.state == "inconclusive"
        assert action.status == "unknown_after_send"
        assert action.claim_token == ""

        second_claim = _set_recovery_claim(action, "second-token")
        session.commit()
        assert service._recover_claimed_unknown_action(
            session,
            second_claim,
            now=_now() + timedelta(minutes=31),
            reprobed_identities=set(),
        ) == 1
        session.flush()

        assert case.state == "remote_confirmed"
        assert action.status == "success"


def test_membership_reprobe_quarantines_business_state_drift(monkeypatch) -> None:
    with _session() as session:
        action, attempt, _task = _seed_unknown_membership(session)
        case = ensure_remote_reconcile_case(session, action, attempt)
        session.commit()
        action.payload = {**dict(action.payload or {}), "channel_id": "-100-drifted"}
        claim = _set_recovery_claim(action, "drift-token")
        session.commit()
        monkeypatch.setattr(
            service.gateway,
            "probe_target_capabilities",
            lambda *_args, **_kwargs: pytest.fail("drifted case must not call Telegram"),
        )

        assert service._recover_claimed_unknown_action(
            session,
            claim,
            now=_now(),
            reprobed_identities=set(),
        ) == 0

        assert case.state == "conflict"
        assert action.status == "unknown_after_send"


def test_legacy_membership_unknown_creates_read_only_recovery_case(monkeypatch) -> None:
    with _session() as session:
        action, attempt, _task = _seed_unknown_membership(
            session,
            with_attempt=False,
        )
        assert attempt is None
        action.payload = {**dict(action.payload or {}), "require_send": False}
        claim = _set_recovery_claim(action, "legacy-token")
        session.commit()
        observed_require_send: list[bool] = []

        def probe(*_args, **kwargs):
            observed_require_send.append(kwargs["require_send"])
            return OperationResult(True, detail="joined")

        monkeypatch.setattr(
            service.gateway,
            "probe_target_capabilities",
            probe,
        )
        monkeypatch.setattr(service, "credentials_for_account", lambda *_args: None)

        assert service._recover_claimed_unknown_action(
            session,
            claim,
            now=_now(),
            reprobed_identities=set(),
        ) == 1

        recovery_attempt = session.scalar(select(ExecutionAttempt).where(
            ExecutionAttempt.action_id == action.id,
        ))
        case = session.scalar(select(RemoteReconcileCase).where(
            RemoteReconcileCase.action_id == action.id,
        ))
        assert recovery_attempt is not None
        assert recovery_attempt.result_snapshot["legacy_unknown_read_only_recovery"] is True
        assert recovery_attempt.status == "success"
        assert case is not None and case.state == "remote_confirmed"
        assert observed_require_send == [False]


def test_legacy_membership_attempt_without_identity_is_preserved(monkeypatch) -> None:
    with _session() as session:
        action, old_attempt, _task = _seed_unknown_membership(session)
        old_attempt.result_snapshot = {}
        action.result = {"error_code": "unknown_after_send"}
        session.commit()
        claim = _set_recovery_claim(action, "legacy-attempt-token")
        session.commit()
        monkeypatch.setattr(
            service.gateway,
            "probe_target_capabilities",
            lambda *_args, **_kwargs: OperationResult(True, detail="joined"),
        )
        monkeypatch.setattr(service, "credentials_for_account", lambda *_args: None)

        assert service._recover_claimed_unknown_action(
            session,
            claim,
            now=_now(),
            reprobed_identities=set(),
        ) == 1

        attempts = list(session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.action_id == action.id)
            .order_by(ExecutionAttempt.attempt_no)
        ))
        assert len(attempts) == 2
        assert attempts[0].id == old_attempt.id
        assert attempts[0].result_snapshot == {}
        assert attempts[1].result_snapshot["source_execution_attempt_id"] == old_attempt.id
        assert attempts[1].result_snapshot["legacy_unknown_read_only_recovery"] is True
        assert attempts[1].status == "success"


def _set_recovery_claim(action: Action, token: str) -> RecoveryClaim:
    action.claim_owner = "recovery:test"
    action.claim_token = token
    action.claim_expires_at = _now() + timedelta(minutes=5)
    return RecoveryClaim(action.id, token)


def _seed_unknown_membership(
    session: Session,
    *,
    with_attempt: bool = True,
):
    session.add(Tenant(id=1, name="t"))
    target = OperationTarget(
        id=81, tenant_id=1, target_type="group", tg_peer_id="-10081",
        title="g", auth_status="已授权运营", can_send=True,
    )
    task = Task(id="task-membership", tenant_id=1, name="m", type="group_ai_chat", status="running")
    account = TgAccount(id=81, tenant_id=1, display_name="a", phone_masked="81", session_ciphertext="s")
    action = Action(
        id="membership-unknown", tenant_id=1, task_id=task.id,
        task_type=task.type, action_type="ensure_target_membership",
        account_id=account.id, status="unknown_after_send", scheduled_at=_now(),
        payload={
            "channel_id": target.tg_peer_id, "channel_target_id": target.id,
            "target_type": "group", "require_send": True,
        },
        result={"error_code": "unknown_after_send"},
    )
    session.add_all([target, task, account, action])
    session.flush()
    attempt = _unknown_attempt(session, action, account) if with_attempt else None
    return action, attempt, task
