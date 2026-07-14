from __future__ import annotations

from app.services._common import gateway

from .ai_generation_dependencies import GenerationDependencies
from .ai_generator import generate_group_messages, generate_group_reply_messages


PRODUCTION_GENERATION_DEPENDENCIES = GenerationDependencies(
    normal_generator=generate_group_messages,
    reply_generator=generate_group_reply_messages,
    reply_target_probe=gateway.probe_target_capabilities,
    reply_messages_fetcher=gateway.fetch_group_messages,
)


__all__ = ["PRODUCTION_GENERATION_DEPENDENCIES"]
