from __future__ import annotations


def assume_default_ai_group_voice_profiles(monkeypatch) -> None:
    from app.models import AiAccountVoiceProfile
    from app.services.task_center.account_voice_profile_cache import (
        VOICE_PROFILE_CONTRACT_VERSION,
        voice_profile_snapshot_hash,
    )
    from app.services.task_center.account_voice_profiles import (
        voice_profile_prompt_details,
    )

    def fake_voice_profile_prompt_details(session, *, tenant_id: int, account_ids: list[int]):
        persisted = voice_profile_prompt_details(
            session,
            tenant_id=tenant_id,
            account_ids=account_ids,
        )
        missing_ids = [int(account_id) for account_id in account_ids if int(account_id) not in persisted]
        for account_id in missing_ids:
            session.add(
                AiAccountVoiceProfile(
                    tenant_id=tenant_id,
                    account_id=account_id,
                    version=1,
                    mask_name=f"测试面具{account_id}",
                    short_prompt_summary=f"账号{account_id}接话，偶尔追问",
                    status="active",
                    quality_status="active",
                )
            )
        if missing_ids:
            session.flush()
            persisted = voice_profile_prompt_details(
                session,
                tenant_id=tenant_id,
                account_ids=account_ids,
            )
        return {
            account_id: {
                **details,
                "snapshot_hash": voice_profile_snapshot_hash(
                    session.get(AiAccountVoiceProfile, details["id"])
                ),
                "contract_version": VOICE_PROFILE_CONTRACT_VERSION,
            }
            for account_id, details in persisted.items()
        }

    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.voice_profile_prompt_details",
        fake_voice_profile_prompt_details,
    )
