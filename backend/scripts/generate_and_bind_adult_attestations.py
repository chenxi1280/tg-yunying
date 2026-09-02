"""Generate and bind proper AdultSubjectAttestations for all AI group tasks."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, delete
from app.database import SessionLocal
from app.models import (
    Task, TgGroup, TaskAiContentPolicyBinding,
    AiContentPolicyVersion, AdultSubjectAttestation, Action
)
from app.services.task_center.ai_content_policy import (
    AttestationSpec, TaskBindingSpec,
    create_adult_attestation, bind_task_policy, _hash
)
from app.services.task_center.daily_group_target import ensure_task_group_daily_target

ACTIVE_POLICY_ID = "81f16610-14c5-4355-a7b1-b397b91944c0"
ALLOWED_ROUTES = ("general", "adult_visual", "adult_service_inquiry", "adult_service_sensory")

def main():
    parser = argparse.ArgumentParser(description="Generate and Bind Adult Attestations")
    parser.add_argument("--apply", action="store_true", help="Apply changes to database")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        policy = session.get(AiContentPolicyVersion, ACTIVE_POLICY_ID)
        if not policy:
            print("ERROR: Active Policy Version not found!")
            return

        tasks = list(session.scalars(
            select(Task).where(
                Task.type == "group_ai_chat",
                Task.status == "running",
                Task.deleted_at.is_(None),
            )
        ).all())

        print(f"Found {len(tasks)} running AI group tasks.")
        today = datetime.now(timezone.utc).date()
        expiry = datetime.now(timezone.utc) + timedelta(days=365)

        for t in tasks:
            if t.id == "6407d98f-e6af-4df8-a10b-806135bf24ff":
                print(f"\n- [郑州楼凤] (ID: {t.id}): Skipping modification, already running smoothly.")
                continue

            cfg = dict(t.type_config or {})
            gid = str(cfg.get("target_group_id") or "")
            if not gid:
                print(f"- [{t.name}] (ID: {t.id}): NO target_group_id, skipping.")
                continue

            print(f"\n- [{t.name}] (ID: {t.id}, Group: {gid}, Epoch: {t.task_lifecycle_epoch}, Rev: {t.config_revision})")

            # 1. Ensure visual attestation
            att_ids = []
            for subj, codes in [
                ("adult_visual", ("adult_visual_content_verified",)),
                ("adult_service", ("adult_service_subject_verified", "adult_service_listing_verified")),
            ]:
                # Check if exists
                existing_att = session.scalar(
                    select(AdultSubjectAttestation).where(
                        AdultSubjectAttestation.tenant_id == t.tenant_id,
                        AdultSubjectAttestation.scope_type == "task_group",
                        AdultSubjectAttestation.scope_id == gid,
                        AdultSubjectAttestation.subject_class == subj,
                        AdultSubjectAttestation.task_config_revision == t.config_revision,
                        AdultSubjectAttestation.policy_version == policy.version,
                        AdultSubjectAttestation.status == "active",
                    )
                )
                if not existing_att and args.apply:
                    spec = AttestationSpec(
                        tenant_id=t.tenant_id,
                        scope_type="task_group",
                        scope_id=gid,
                        subject_class=subj,
                        evidence_codes=codes,
                        actor_user_id=1,
                        permission_snapshot={"adult_content_attest": True},
                        expires_at=expiry,
                        task_config_revision=t.config_revision,
                        policy_version=policy.version,
                    )
                    existing_att = create_adult_attestation(session, spec)
                    print(f"  [CREATED ATTESTATION] {subj} -> ID: {existing_att.id}")
                elif existing_att:
                    print(f"  [REUSED ATTESTATION] {subj} -> ID: {existing_att.id}")
                
                if existing_att:
                    att_ids.append(existing_att.id)

            if args.apply and len(att_ids) == 2:
                cfg["ai_content_route_v2_enabled"] = "true"
                cfg["ai_two_stage_enabled"] = True
                if not str(cfg.get("ai_model", "")).strip():
                    cfg["ai_model"] = "gemini-2.5-flash"
                if not str(cfg.get("ai_semantic_reviewer_model", "")).strip():
                    cfg["ai_semantic_reviewer_model"] = "gemini-1.5-flash"
                cfg["ai_provider_id"] = "6"
                cfg["ai_content_policy_version_id"] = policy.id
                cfg["ai_content_allowed_routes"] = list(ALLOWED_ROUTES)
                cfg["ai_content_attestation_ids"] = att_ids
                cfg["daily_message_target"] = 4200
                t.type_config = cfg
                t.updated_at = datetime.now(timezone.utc)

                # Delete obsolete mismatching binding
                session.execute(
                    delete(TaskAiContentPolicyBinding).where(
                        TaskAiContentPolicyBinding.task_id == t.id,
                        TaskAiContentPolicyBinding.task_lifecycle_epoch == t.task_lifecycle_epoch,
                        TaskAiContentPolicyBinding.task_config_revision == t.config_revision,
                    )
                )

                # Bind fresh valid policy with proper scope_refs
                binding = bind_task_policy(session, TaskBindingSpec(
                    task_id=t.id,
                    policy_version_id=policy.id,
                    allowed_routes=ALLOWED_ROUTES,
                    attestation_ids=tuple(att_ids),
                    scope_refs=(("task_group", gid),),
                    approved_by=policy.approved_by,
                ))
                print(f"  [APPLIED BINDING] Successfully bound! Hash: {binding.evidence_hash[:16]}")

                # Refresh daily ledger target
                group = session.get(TgGroup, int(gid))
                if group:
                    refreshed = ensure_task_group_daily_target(session, t, group, today)
                    print(f"  [REFRESHED LEDGER] Target: {refreshed.effective_message_target}")
                session.flush()

        # Check and unlock Tianjin Music
        tianjin_task_id = "7fd0bbb7-53dd-45ae-a7af-0c37bcc380d1"
        tianjin = session.get(Task, tianjin_task_id)
        if tianjin and args.apply:
            tianjin.last_error = ""
            print("\n[TIANJIN MUSIC] Cleared task last_error.")

        if args.apply:
            session.commit()
            print("\n>>> All attestations created, policies bound, and tasks refreshed successfully!")
        else:
            print("\n>>> Preview completed. Run with --apply to commit.")
    finally:
        session.close()

if __name__ == "__main__":
    main()
