"""Production OpenAI adapter — same contract as the simulated providers.

Text-path integration via the Responses/Chat Completions API for translation,
ASL gloss, and simplification. In production the conversational/live classes
move to the Realtime API over WebRTC/WebSocket for streaming audio; that swap
happens inside this file only — the engine never changes (spec §1.6).

Activated automatically by default_registry() when OPENAI_API_KEY is set;
otherwise the simulated providers stand in so demos and tests run anywhere.
"""
from __future__ import annotations

import os

import httpx

from .models import LANGUAGE_NAMES, Artifact, ContentEvent, DeliveryPlan, Modality
from .providers import Provider, ProviderError

API_URL = "https://api.openai.com/v1/chat/completions"

RULES = (" Preserve the exact meaning. Never add information, explanations, greetings, or"
         " commentary. Always produce the output in the target language, even if the input"
         " is a single word. Output only the result, nothing else.")

INSTRUCTIONS = {
    Modality.translation: "Translate the announcement into {lang}." + RULES,
    Modality.captions: "Translate the announcement into {lang} as concise captions." + RULES,
    Modality.speech: "Translate the announcement into {lang} for text-to-speech delivery." + RULES,
    Modality.sign: ("Render the announcement as {lang} sign-language gloss, the uppercase gloss"
                    " notation a sign-synthesis engine consumes." + RULES),
    Modality.simplified: ("Translate the announcement into {lang}, rewritten to plain-language standards:"
                          " short sentences, one idea per sentence, common everyday words, active voice."
                          + RULES),
    Modality.audio_description: "Write a brief {lang} audio description of the announcement." + RULES,
}


class OpenAIProvider(Provider):
    def __init__(self, name: str = "ai-realtime", model: str = "gpt-4o-mini",
                 api_key: str | None = None):
        super().__init__()
        self.name = name
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    async def render(self, plan: DeliveryPlan, event: ContentEvent) -> Artifact:
        if not self.api_key:
            raise ProviderError("no API key configured")
        if not (event.payload or "").strip():
            raise ProviderError("template event with no free text; cache only")
        instruction = INSTRUCTIONS[plan.modality].format(lang=LANGUAGE_NAMES.get(plan.language, plan.language))
        try:
            async with httpx.AsyncClient(timeout=plan.e2e_budget_ms / 1000) as client:
                resp = await client.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model,
                          "messages": [{"role": "system", "content": instruction},
                                       {"role": "user", "content": event.payload}],
                          "max_tokens": 300})
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"upstream {resp.status_code}")
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return Artifact(plan_id=plan.id, modality=plan.modality, language=plan.language,
                        content=text, provider=self.name,
                        # Production: reference-free QE model scores each artifact
                        # (spec §2.5); a static prior stands in until then.
                        quality_estimate=0.93)
