from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AiProviderCredentials:
    provider_name: str
    provider_type: str
    base_url: str
    model_name: str
    api_key: str
    api_key_header: str = "Authorization"


@dataclass(frozen=True)
class AiDraftCandidate:
    persona: str
    content: str
    risk_level: str = "低"
    material_id: int | None = None
    suggested_account_id: int | None = None
    sequence_index: int = 0
    reply_to_sequence_index: int | None = None


@dataclass(frozen=True)
class AiUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    billable: bool = False


@dataclass(frozen=True)
class AiGenerationResult:
    candidates: list[AiDraftCandidate]
    usage: AiUsage


class AiEmptyFinalContentError(RuntimeError):
    def __init__(self, detail: str, *, retryable_reasoning_length: bool) -> None:
        super().__init__(detail)
        self.retryable_reasoning_length = retryable_reasoning_length


MODEL_ALIASES = {
    "deepseek v4 flash": "deepseek-v4-flash",
    "deepseek-v4-flash": "deepseek-v4-flash",
    "deepseek v4 pro": "deepseek-v4-pro",
    "deepseek-v4-pro": "deepseek-v4-pro",
    "mimo v2.5": "mimo-v2.5",
    "mimo-v2.5": "mimo-v2.5",
    "xiaomi mimo-v2.5": "mimo-v2.5",
    "mimo v2.5 pro": "mimo-v2.5-pro",
    "mimo-v2.5-pro": "mimo-v2.5-pro",
    "xiaomi mimo-v2.5-pro": "mimo-v2.5-pro",
}


def normalize_ai_model_name(model_name: str) -> str:
    normalized = " ".join(model_name.strip().split()).lower()
    return MODEL_ALIASES.get(normalized, model_name.strip())


def mock_candidates(
    count: int,
    topic: str,
    tone: str,
    persona_set: list[str],
    material_ids: list[int] | None = None,
    selected_account_ids: list[int] | None = None,
) -> list[AiDraftCandidate]:
    templates, include_suffix = _mock_templates_for_tone(tone)
    ids = material_ids or []
    account_ids = selected_account_ids or []
    candidates: list[AiDraftCandidate] = []
    for index in range(count):
        material_id = ids[index % len(ids)] if ids else None
        suggested_account_id = account_ids[index % len(account_ids)] if account_ids else None
        suffix = _mock_candidate_suffix(topic, tone, material_id) if include_suffix else ""
        candidates.append(
            AiDraftCandidate(
                persona=persona_set[index % len(persona_set)],
                content=f"{templates[index % len(templates)]}{suffix}",
                risk_level="低",
                material_id=material_id,
                suggested_account_id=suggested_account_id,
                sequence_index=index + 1,
                reply_to_sequence_index=index if index else None,
            )
        )
    return candidates


def _mock_templates_for_tone(tone: str) -> tuple[list[str], bool]:
    if "频道评论" in tone:
        return [
            "这个细节挺具体",
            "评论区有人试过吗",
            "这点确实容易忽略",
            "原文这个例子挺直观",
            "后面会补更多吗",
        ], False
    return [
        "我看这个点其实挺像群里前两天聊的那个情况。",
        "先别拉太大，具体到今天这个场景会好聊一点。",
        "要是新来的朋友问，我可能会先让他看最容易卡住的那一步。",
        "这事我更关心实际反馈，纸面说法有时候不太准。",
        "可以慢慢聊，别一下子把话题弄得太硬。",
    ], True


def _mock_candidate_suffix(topic: str, tone: str, material_id: int | None) -> str:
    suffix = f"（话题：{topic}，语气：{tone}）"
    if material_id:
        suffix += f" [建议素材 #{material_id}]"
    return suffix


class AiGateway:
    def generate_drafts(
        self,
        credentials: AiProviderCredentials,
        prompt: str,
        *,
        count: int,
        topic: str,
        tone: str,
        persona_set: list[str],
        temperature: float,
        max_tokens: int,
        material_ids: list[int] | None = None,
        selected_account_ids: list[int] | None = None,
        system_prompt: str | None = None,
    ) -> AiGenerationResult:
        if credentials.base_url.startswith("mock://"):
            return AiGenerationResult(
                candidates=mock_candidates(count, topic, tone, persona_set, material_ids, selected_account_ids),
                usage=AiUsage(),
            )
        if credentials.provider_type != "openai_compatible":
            raise RuntimeError(f"unsupported ai provider type: {credentials.provider_type}")

        raw, usage = self._post_openai_compatible(
            credentials,
            prompt,
            temperature,
            max_tokens,
            system_prompt=system_prompt or "你是一个 Telegram 群运营话术助手，只输出用户要求的 JSON。",
            response_format_json=True,
            reasoning_retry_max_tokens=self._generation_retry_max_tokens(credentials, max_tokens, count),
        )
        return AiGenerationResult(
            candidates=self._parse_candidates(raw, count, persona_set, material_ids),
            usage=usage,
        )

    def check(self, credentials: AiProviderCredentials) -> tuple[bool, str]:
        if credentials.base_url.startswith("mock://"):
            return True, "mock provider ready"
        try:
            self._post_openai_compatible(
                credentials,
                "请直接回复 OK，不要解释，不要推理过程。",
                0.1,
                256,
                system_prompt="You are a health-check probe. Reply with exactly OK and no other text.",
            )
            warning = self._check_chat_capability(credentials)
        except Exception as exc:  # noqa: BLE001 - stored as operator-facing health detail.
            return False, str(exc)
        if warning:
            return True, warning
        return True, "provider ready; chat capability ready"

    def _check_chat_capability(self, credentials: AiProviderCredentials) -> str:
        try:
            raw, _ = self._post_openai_compatible(
                credentials,
                '只输出这个 JSON，不要解释：{"drafts":[{"content":"OK"}]}',
                0.1,
                512,
                system_prompt="Output compact valid JSON only. No analysis.",
                response_format_json=True,
                reasoning_retry_max_tokens=2048,
            )
            self._parse_candidates(raw, 1, ["A"], None)
        except AiEmptyFinalContentError as exc:
            if exc.retryable_reasoning_length:
                return f"provider ready; chat capability warning: {exc}"
            raise
        return ""

    def _post_openai_compatible(
        self,
        credentials: AiProviderCredentials,
        prompt: str,
        temperature: float,
        max_tokens: int,
        *,
        system_prompt: str = "你是一个 Telegram 群运营话术助手，只输出用户要求的 JSON。",
        response_format_json: bool = False,
        reasoning_retry_max_tokens: int | None = None,
        timeout: int = 30,
    ) -> tuple[str, AiUsage]:
        url = self._chat_completions_url(credentials.base_url)
        headers = {"Content-Type": "application/json"}
        if credentials.api_key_header.lower() == "authorization":
            headers["Authorization"] = f"Bearer {credentials.api_key}"
        else:
            headers[credentials.api_key_header] = credentials.api_key
        attempt_tokens = [max_tokens]
        if reasoning_retry_max_tokens and reasoning_retry_max_tokens > max_tokens:
            attempt_tokens.append(reasoning_retry_max_tokens)
        last_empty_error: AiEmptyFinalContentError | None = None
        for token_budget in attempt_tokens:
            payload = self._chat_payload(credentials, prompt, system_prompt, temperature, token_budget, response_format_json)
            request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    data = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="ignore")
                raise RuntimeError(f"AI provider HTTP {exc.code}: {detail[:300]}") from exc
            content = self._extract_message_content(data)
            if content:
                return content, self._usage_from_payload(data)
            last_empty_error = AiEmptyFinalContentError(
                self._empty_content_detail(data),
                retryable_reasoning_length=self._is_reasoning_length_empty(data),
            )
            if not last_empty_error.retryable_reasoning_length:
                break
        if last_empty_error:
            raise last_empty_error
        raise RuntimeError("AI provider returned no choices")

    def _chat_payload(
        self,
        credentials: AiProviderCredentials,
        prompt: str,
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        response_format_json: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": credentials.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if self._is_deepseek(credentials):
            payload["thinking"] = {"type": "disabled"}
        if response_format_json and self._is_deepseek(credentials):
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _generation_retry_max_tokens(self, credentials: AiProviderCredentials, max_tokens: int, count: int) -> int:
        base_retry = max(max_tokens, count * 512, 2048)
        if self._is_mimo(credentials):
            return max(base_retry, count * 768, 4096)
        return base_retry

    def _chat_completions_url(self, base_url: str) -> str:
        url = base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if self._is_deepseek_base_url(url):
            if url.endswith("/v1"):
                url = url[:-3]
            return f"{url}/chat/completions"
        if url.endswith("/v1"):
            return f"{url}/chat/completions"
        return f"{url}/v1/chat/completions"

    def _is_deepseek(self, credentials: AiProviderCredentials) -> bool:
        return credentials.model_name.startswith("deepseek-") or self._is_deepseek_base_url(credentials.base_url)

    def _is_deepseek_base_url(self, base_url: str) -> bool:
        return "api.deepseek.com" in base_url.lower()

    def _is_mimo(self, credentials: AiProviderCredentials) -> bool:
        return "mimo" in credentials.model_name.lower() or "mimo" in credentials.provider_name.lower() or "xiaomimimo" in credentials.base_url.lower()

    def _extract_message_content(self, data: dict[str, Any]) -> str:
        choice = self._first_choice(data)
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                    elif isinstance(item.get("content"), str):
                        parts.append(str(item["content"]))
            return "".join(parts).strip()
        return ""

    def _empty_content_detail(self, data: dict[str, Any]) -> str:
        choice = self._first_choice(data)
        message = choice.get("message") if isinstance(choice, dict) else None
        detail_parts = ["AI provider returned empty final content"]
        if isinstance(choice, dict) and choice.get("finish_reason"):
            detail_parts.append(f"finish_reason={choice['finish_reason']}")
        usage_payload = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage_payload, dict):
            prompt_tokens = usage_payload.get("prompt_tokens", 0) or 0
            completion_tokens = usage_payload.get("completion_tokens", 0) or 0
            total_tokens = usage_payload.get("total_tokens", 0) or 0
            detail_parts.append(f"usage=prompt:{prompt_tokens}, completion:{completion_tokens}, total:{total_tokens}")
        reasoning_fields: list[str] = []
        if isinstance(message, dict):
            for field in ["reasoning_content", "reasoning", "reasoning_details"]:
                value = message.get(field)
                if value:
                    reasoning_fields.append(f"{field} present ({len(str(value))} chars)")
        if reasoning_fields:
            detail_parts.append("; ".join(reasoning_fields))
            detail_parts.append("model produced reasoning but no final answer; retry used a higher max_tokens budget; try an even higher provider limit or a non-reasoning model")
        return "; ".join(detail_parts)

    def _is_reasoning_length_empty(self, data: dict[str, Any]) -> bool:
        choice = self._first_choice(data)
        if not isinstance(choice, dict) or choice.get("finish_reason") != "length":
            return False
        message = choice.get("message")
        if not isinstance(message, dict):
            return False
        return any(bool(message.get(field)) for field in ["reasoning_content", "reasoning", "reasoning_details"])

    def _first_choice(self, data: dict[str, Any]) -> dict[str, Any]:
        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            return choices[0]
        return {}

    def _usage_from_payload(self, data: dict[str, Any]) -> AiUsage:
        usage_payload = data.get("usage") or {}
        prompt_tokens = int(usage_payload.get("prompt_tokens") or 0)
        completion_tokens = int(usage_payload.get("completion_tokens") or 0)
        total_tokens = int(usage_payload.get("total_tokens") or (prompt_tokens + completion_tokens))
        return AiUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            billable=bool(total_tokens > 0),
        )

    def _parse_candidates(self, raw: str, count: int, persona_set: list[str], material_ids: list[int] | None) -> list[AiDraftCandidate]:
        clean = raw.strip()
        if clean.startswith("```"):
            # Remove opening code fence (```json or ```)
            clean = clean.lstrip("`")
            if clean.startswith("json"):
                clean = clean[4:].strip()
            # Remove closing code fence
            if clean.endswith("```"):
                clean = clean.rstrip("`").strip()
        try:
            parsed: Any = json.loads(clean)
        except json.JSONDecodeError:
            if _looks_like_json_drafts_fragment(clean):
                raise RuntimeError("AI provider returned malformed JSON drafts")
            lines = [line.strip(" -\t") for line in clean.splitlines() if line.strip()]
            return [
                AiDraftCandidate(persona=persona_set[index % len(persona_set)], content=line[:1000], risk_level="低")
                for index, line in enumerate(lines[:count])
            ]
        items = parsed.get("drafts", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(items, list):
            raise RuntimeError("AI provider JSON must be a list or {drafts: [...]}")
        candidates: list[AiDraftCandidate] = []
        fallback_materials = material_ids or []
        for index, item in enumerate(items[:count]):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or item.get("message") or "").strip()
            if not content:
                continue
            material_id = item.get("material_id")
            if not isinstance(material_id, int):
                material_id = fallback_materials[index % len(fallback_materials)] if fallback_materials else None
            candidates.append(
                AiDraftCandidate(
                    persona=str(item.get("persona") or persona_set[index % len(persona_set)]),
                    content=content[:2000],
                    risk_level=str(item.get("risk_level") or "低"),
                    material_id=material_id,
                    suggested_account_id=item.get("suggested_account_id") if isinstance(item.get("suggested_account_id"), int) else None,
                    sequence_index=item.get("sequence_index") if isinstance(item.get("sequence_index"), int) else index + 1,
                    reply_to_sequence_index=(
                        item.get("reply_to_sequence_index")
                        if isinstance(item.get("reply_to_sequence_index"), int)
                        else item.get("reply_to_sequence")
                        if isinstance(item.get("reply_to_sequence"), int)
                        else None
                    ),
                )
            )
        if not candidates:
            raise RuntimeError("AI provider returned no usable drafts")
        return candidates


def _looks_like_json_drafts_fragment(value: str) -> bool:
    clean = value.strip().lstrip("\ufeff")
    return (
        clean.startswith(("{", "["))
        or '"drafts"' in clean
        or "'drafts'" in clean
        or "risk_level" in clean
        or "sequence_index" in clean
    )


def create_ai_gateway() -> AiGateway:
    return AiGateway()
