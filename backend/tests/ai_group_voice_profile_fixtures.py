from __future__ import annotations


def assume_default_ai_group_voice_profiles(monkeypatch) -> None:
    from app.services.task_center.account_voice_profiles import voice_profile_prompt_details

    def fake_voice_profile_prompt_details(session, *, tenant_id: int, account_ids: list[int]):
        persisted = voice_profile_prompt_details(
            session,
            tenant_id=tenant_id,
            account_ids=account_ids,
        )
        fallback = {
            int(account_id): {
                "version": 1,
                "summary": f"账号{int(account_id)}接话，偶尔追问",
            }
            for account_id in account_ids if int(account_id) not in persisted
        }
        return {**fallback, **persisted}

    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.voice_profile_prompt_details",
        fake_voice_profile_prompt_details,
    )
