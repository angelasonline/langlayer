"""Anthropic Claude adapter, same contract as every other provider.

Activated by ANTHROPIC_API_KEY. When present, Claude runs as the primary
translation source and OpenAI becomes the failover peer (or vice versa),
making the chain genuinely multi-provider.
"""
from __future__ import annotations

import os

import httpx

from .models import LANGUAGE_NAMES, Artifact, ContentEvent, DeliveryPlan
from .providers import Provider, ProviderError
from .providers_openai import INSTRUCTIONS

API_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(Provider):
    def __init__(self, name: str = "claude-fable-5", model: str = "claude-fable-5",
                 api_key: str | None = None):
        super().__init__()
        self.name = name
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    async def render(self, plan: DeliveryPlan, event: ContentEvent) -> Artifact:
        if not self.api_key:
            raise ProviderError("no API key configured")
        instruction = INSTRUCTIONS[plan.modality].format(
            lang=LANGUAGE_NAMES.get(plan.language, plan.language))
        try:
            async with httpx.AsyncClient(timeout=plan.e2e_budget_ms / 1000) as client:
                resp = await client.post(
                    API_URL,
                    headers={"x-api-key": self.api_key,
                             "anthropic-version": "2023-06-01"},
                    json={"model": self.model, "max_tokens": 300,
                          "system": instruction,
                          "messages": [{"role": "user", "content": event.payload}]})
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"upstream {resp.status_code}")
        blocks = resp.json().get("content", [])
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
        if not text:
            raise ProviderError("empty response")
        return Artifact(plan_id=plan.id, modality=plan.modality, language=plan.language,
                        content=text, provider=self.name, quality_estimate=0.94)
