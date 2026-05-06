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


def mock_candidates(
    count: int,
    topic: str,
    tone: str,
    persona_set: list[str],
    material_ids: list[int] | None = None,
    selected_account_ids: list[int] | None = None,
) -> list[AiDraftCandidate]:
    templates = [
        "刚看了下置顶，这个话题可以先从大家最关心的点聊起。",
        "接上面说的，我更想听听已经体验过的朋友反馈，实际用起来有没有明显变化？",
        "这个问题可以拆开聊，先把新手最容易卡住的地方整理出来。",
        "我补一句，如果今晚有人继续问，我可以把常见问题顺手汇总下。",
        "感觉今天适合轻量讨论，不用刷屏，有问题慢慢抛出来就行。",
    ]
    ids = material_ids or []
    account_ids = selected_account_ids or []
    candidates: list[AiDraftCandidate] = []
    for index in range(count):
        material_id = ids[index % len(ids)] if ids else None
        suggested_account_id = account_ids[index % len(account_ids)] if account_ids else None
        suffix = f"（话题：{topic}，语气：{tone}）"
        if material_id:
            suffix += f" [建议素材 #{material_id}]"
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
    ) -> AiGenerationResult:
        if credentials.base_url.startswith("mock://"):
            return AiGenerationResult(
                candidates=mock_candidates(count, topic, tone, persona_set, material_ids, selected_account_ids),
                usage=AiUsage(),
            )
        if credentials.provider_type != "openai_compatible":
            raise RuntimeError(f"unsupported ai provider type: {credentials.provider_type}")

        raw, usage = self._post_openai_compatible(credentials, prompt, temperature, max_tokens)
        return AiGenerationResult(
            candidates=self._parse_candidates(raw, count, persona_set, material_ids),
            usage=usage,
        )

    def check(self, credentials: AiProviderCredentials) -> tuple[bool, str]:
        if credentials.base_url.startswith("mock://"):
            return True, "mock provider ready"
        try:
            self._post_openai_compatible(credentials, "请只回复 OK。", 0.1, 32)
        except Exception as exc:  # noqa: BLE001 - stored as operator-facing health detail.
            return False, str(exc)
        return True, "provider ready"

    def _post_openai_compatible(self, credentials: AiProviderCredentials, prompt: str, temperature: float, max_tokens: int) -> tuple[str, AiUsage]:
        url = credentials.base_url.rstrip("/")
        if url.endswith("/chat/completions"):
            pass  # already the full endpoint URL
        elif url.endswith("/v1"):
            url = f"{url}/chat/completions"
        else:
            url = f"{url}/v1/chat/completions"
        payload = {
            "model": credentials.model_name,
            "messages": [
                {"role": "system", "content": "你是一个 Telegram 群运营话术助手，只输出用户要求的 JSON。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if credentials.api_key_header.lower() == "authorization":
            headers["Authorization"] = f"Bearer {credentials.api_key}"
        else:
            headers[credentials.api_key_header] = credentials.api_key
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"AI provider HTTP {exc.code}: {detail[:300]}") from exc
        content = data.get("choices", [{}])[0].get("message", {}).get("content")
        if not content:
            raise RuntimeError("AI provider returned empty content")
        usage_payload = data.get("usage") or {}
        prompt_tokens = int(usage_payload.get("prompt_tokens") or 0)
        completion_tokens = int(usage_payload.get("completion_tokens") or 0)
        total_tokens = int(usage_payload.get("total_tokens") or (prompt_tokens + completion_tokens))
        usage = AiUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            billable=bool(total_tokens > 0),
        )
        return str(content), usage

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


def create_ai_gateway() -> AiGateway:
    return AiGateway()
