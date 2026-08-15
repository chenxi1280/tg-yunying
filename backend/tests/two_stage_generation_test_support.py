from __future__ import annotations


def brief_payload(slot_id: str, **overrides) -> dict:
    item = {
        "slot_id": slot_id,
        "speech_act": "follow_up",
        "stance": "curious",
        "length_band": "short",
        "punctuation_profile": "none",
        "anchor_ids": ["f1"],
    }
    item.update(overrides)
    return item


def planner_factory(payloads_or_plans):
    queue = list(payloads_or_plans)
    calls: list[dict] = []

    def planner(session, tenant_id, config, *, system_prompt, user_prompt, count):
        calls.append({"user_prompt": user_prompt, "count": count})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return {"briefs": item}, 10

    planner.calls = calls
    return planner


def realizer_factory(outputs):
    queue = list(outputs)
    calls: list[dict] = []

    def realizer(session, tenant_id, config, *, system_prompt, user_prompt):
        calls.append({"user_prompt": user_prompt})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, 5

    realizer.calls = calls
    return realizer


def reviewer_factory(decision: str = "pass", *, code: str = ""):
    def reviewer(session, tenant_id, config, *, system_prompt, user_prompt):
        return {
            "decision": decision,
            "confidence": 0.95,
            "codes": [code] if code else [],
            "evidence": [{"criterion": "context", "observed": "anchor checked"}],
            "prompt_version": "semantic_reviewer_v1",
        }, 3

    return reviewer
