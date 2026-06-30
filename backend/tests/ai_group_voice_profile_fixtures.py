from __future__ import annotations


def assume_default_ai_group_voice_profiles(monkeypatch) -> None:
    def fake_voice_profile_prompt_details(_session, *, tenant_id: int, account_ids: list[int]):
        return {
            int(account_id): {
                "version": 1,
                "summary": f"账号{int(account_id)}短句接话，少总结，偶尔追问",
            }
            for account_id in account_ids
        }

    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.voice_profile_prompt_details",
        fake_voice_profile_prompt_details,
    )
