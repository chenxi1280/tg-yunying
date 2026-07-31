from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.orm import Session

from app.services.task_center.ai_generator import AiGenerationUnavailable, GeneratedContent
from app.services.task_center.ai_generation_dependencies import GenerationDependencies


def _request(
    content: str,
    *,
    account_profile: str = "",
    stance_summary: str = "",
    cached: bool = True,
    **updates,
):
    values = {
        "batch_ids": ["action-1"],
        "cached_contents": [
            GeneratedContent(content, slot_id="slot-1", sequence_index=1)
        ] if cached else [],
        "cached_tokens": 0,
        "duplicate_baseline_messages": [],
        "quality_snapshots": [{"account_profile": account_profile, "stance_summary": stance_summary}],
        "config": {"generation_slots": [{"slot_id": "slot-1", "account_id": 11}]},
        "chat_mode": "reply",
        "context_message_ids": [1],
        "fact_anchor_required": True,
        "low_confidence_silence_enabled": True,
        "is_reply": False,
        "tenant_id": 1,
        "reply_targets": [],
        "target_label": "运营群",
        "history": "真人用户: 今天聊聊",
    }
    if "stance_summary" in updates:
        values["quality_snapshots"][0]["stance_summary"] = updates.pop("stance_summary")
    values.update(updates)
    return SimpleNamespace(**values)


def _stage_generator(session: Session, observed: list[str]):
    def generate(_session, _tenant_id, config, **_kwargs):
        assert session.in_transaction() is False
        observed.append(str(config.get("_ai_fallback_stage") or "direct"))
        return [
            GeneratedContent(
                "😂😂",
                slot_id=slot["slot_id"],
                sequence_index=index,
            )
            for index, slot in enumerate(config["generation_slots"], 1)
        ], 1

    return generate


def _coverage_slot(slot_id: str, account_id: int) -> dict:
    return {
        "slot_id": slot_id,
        "account_id": account_id,
        "group_id": 2,
        "coverage_ledger_id": f"coverage-{account_id}",
        "coverage_window_date": "2026-07-16",
    }


def _quantity_slot(slot_id: str, account_id: int) -> dict:
    return {
        "slot_id": slot_id,
        "account_id": account_id,
        "group_id": 2,
        "primary_quantity_slot_id": f"quantity-{account_id}",
        "coverage_ledger_id": "",
        "content_obligation_fallback_ready": True,
    }


def _forbidden_generator(*_args, **_kwargs):
    raise AssertionError("cached quality validation must not call a provider")


def _unavailable_generator(*_args, **_kwargs):
    raise AiGenerationUnavailable("provider unavailable")


def _dependencies(
    *,
    normal_generator=_forbidden_generator,
    reply_generator=_forbidden_generator,
) -> GenerationDependencies:
    return GenerationDependencies(
        normal_generator=normal_generator,
        reply_generator=reply_generator,
        reply_target_probe=_forbidden_generator,
        reply_messages_fetcher=_forbidden_generator,
    )
