from __future__ import annotations

from app.database import SessionLocal
from app.models import (
    Action,
    AiProvider,
    ExecutionAttempt,
    FulfillmentRemoteFact,
    RemoteMutationTombstone,
    Task,
    TaskDeleteOperation,
    Tenant,
    TenantAiSetting,
)
from app.services._common import _now
from app.services.task_center.fulfillment_activation import (
    ActivationRequest,
    activate_manifest,
    clone_prepared_task,
    prepare_activation_manifest,
    preview_activation,
)
from app.services.task_center.physical_task_deletion import (
    DeleteRequest,
    advance_task_deletion,
    prepare_task_deletion,
)


TENANT_ID = 1


def _remote_chain(task: Task, suffix: str) -> tuple[Action, ExecutionAttempt, FulfillmentRemoteFact]:
    action = Action(
        id=f"action-{suffix}",
        tenant_id=TENANT_ID,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="success",
        obligation_type="coverage",
        obligation_id=f"obligation-{suffix}",
        action_dedupe_key=f"dedupe-{suffix}",
    )
    attempt = ExecutionAttempt(
        id=f"attempt-{suffix}",
        tenant_id=TENANT_ID,
        action_id=action.id,
        status="success",
        gateway_call_started_at=_now(),
        after_call_at=_now(),
        remote_message_id=f"message-{suffix}",
    )
    fact = FulfillmentRemoteFact(
        fact_id=f"fact-{suffix}",
        tenant_id=TENANT_ID,
        task_type=task.type,
        task_id=task.id,
        obligation_type=action.obligation_type,
        obligation_id=action.obligation_id,
        action_id=action.id,
        attempt_id=attempt.id,
        mutation_kind=action.action_type,
        remote_mutation_key_hash=(suffix[0] * 64),
        gateway_request_hash=(suffix[-1] * 64),
        fact_kind="remote_message_observed",
        fact_identity_hash=("f" * 63) + suffix[-1],
        outcome={"remote_message_id": attempt.remote_message_id},
    )
    return action, attempt, fact


def test_physical_delete_resumes_stages_and_preserves_only_remote_tombstone() -> None:
    with SessionLocal() as session:
        if session.get(Tenant, TENANT_ID) is None:
            session.add(Tenant(id=TENANT_ID, name="物理删除集成测试"))
            session.flush()
        if session.get(AiProvider, 999991) is None:
            session.add(AiProvider(
                id=999991,
                provider_name="physical-delete-test-provider",
                base_url="https://provider.invalid",
                model_name="test-model",
                api_key_ciphertext="encrypted-test-key",
            ))
            session.flush()
        setting = session.query(TenantAiSetting).filter_by(tenant_id=TENANT_ID).one_or_none()
        if setting is None:
            session.add(TenantAiSetting(
                id=999991,
                tenant_id=TENANT_ID,
                default_provider_id=999991,
                ai_enabled=True,
            ))
        else:
            setting.default_provider_id = 999991
            setting.ai_enabled = True
        session.flush()
        old = Task(
            id="old-delete-v3",
            tenant_id=TENANT_ID,
            name="旧任务",
            type="group_ai_chat",
            status="running",
            fulfillment_contract_version="legacy_v1",
            account_config={"selection_mode": "all"},
            type_config={"daily_message_target": 1},
        )
        session.add(old)
        session.flush()
        new = clone_prepared_task(session, old, actor_id=None)
        old_chain = _remote_chain(old, "old1")
        canary_chain = _remote_chain(new, "new2")
        session.add_all([*old_chain, *canary_chain])
        session.flush()
        preview = preview_activation(
            session,
            tenant_id=TENANT_ID,
            old_task_ids=(old.id,),
            new_task_ids=(new.id,),
        )
        manifest = prepare_activation_manifest(session, ActivationRequest(
            tenant_id=TENANT_ID,
            release_train="physical-delete-v3",
            old_task_ids=(old.id,),
            new_task_ids=(new.id,),
            canary_task_id=new.id,
            expected_old_set_hash=preview.old_set_hash,
            expected_new_config_set_hash=preview.new_config_set_hash,
            approval_ref="test-approved",
        ))
        session.flush()
        activate_manifest(session, manifest.id, expected_version=1)
        session.commit()
        manifest_id = manifest.id
        manifest_hash = manifest.old_set_hash
        old_id = old.id
        old_action_id = old_chain[0].id
        old_attempt_id = old_chain[1].id
        old_fact_id = old_chain[2].fact_id

    with SessionLocal() as session:
        operation = prepare_task_deletion(session, DeleteRequest(
            task_id=old_id,
            manifest_id=manifest_id,
            expected_manifest_hash=manifest_hash,
            actor="pytest",
            approval_ref="test-approved",
        ))
        session.commit()
        operation_id = operation.id
        assert session.get(Task, old_id).status == "deleting"

    expected_states = [
        "snapshot_committed",
        "tombstones_written",
        "tombstone_verified",
        "deleting",
        "committed",
    ]
    for expected_state in expected_states:
        with SessionLocal() as session:
            current = session.get(TaskDeleteOperation, operation_id)
            advanced = advance_task_deletion(
                session,
                operation_id,
                expected_stage_version=current.stage_version,
            )
            session.commit()
            assert advanced.state == expected_state

    with SessionLocal() as session:
        assert session.get(Task, old_id) is None
        assert session.get(Action, old_action_id) is None
        assert session.get(ExecutionAttempt, old_attempt_id) is None
        assert session.get(FulfillmentRemoteFact, old_fact_id) is None
        assert session.get(TaskDeleteOperation, operation_id).state == "committed"
        tombstones = session.query(RemoteMutationTombstone).filter_by(
            original_task_id=old_id
        ).all()
        assert len(tombstones) == 1
        assert tombstones[0].remote_fact_identity_hash
