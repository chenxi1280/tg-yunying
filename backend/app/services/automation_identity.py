"""Identity instructions for natural persona engagement."""

AUTOMATION_IDENTITY_POLICY_VERSION = "natural_persona_v1"
AUTOMATION_IDENTITY_SYSTEM_POLICY = ""


def with_automation_identity(system_prompt: str | None) -> str:
    """Return system prompt without injecting forced automation disclosure."""
    prompt = (system_prompt or "").strip()
    if not AUTOMATION_IDENTITY_SYSTEM_POLICY:
        return prompt
    if prompt.endswith(AUTOMATION_IDENTITY_SYSTEM_POLICY):
        return prompt
    return f"{prompt}\n\n{AUTOMATION_IDENTITY_SYSTEM_POLICY}".strip()
