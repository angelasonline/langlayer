"""Mesh LLM adapter — local-model tier for offline resilience.

Mesh LLM (github.com/Mesh-LLM/mesh-llm) pools local machines on a LAN and
exposes them as one OpenAI-compatible endpoint (default localhost:9337/v1).
Because the API is OpenAI-compatible, this adapter mirrors
providers_openai.py with three deliberate differences:

1. Base URL points at the local mesh (MESH_BASE_URL), not the cloud.
2. A fast health probe: if the mesh is unreachable we fail in ~400ms so the
   circuit breaker trips immediately and failover stays snappy — this tier
   must never slow down the chain when it is absent.
3. Honest quality prior: local open models are weaker at low-resource
   languages and ASL gloss than frontier cloud models. This is the
   degraded-but-present tier; receipts carry a lower prior and the offline
   runbook says so out loud.

The model is a fire extinguisher: staged onto the mesh while online, ready
when the internet is gone. Activated by default_registry() when
MESH_BASE_URL (or MESH_ENABLED=1) is set; otherwise absent, and every
existing deployment is unchanged.
"""
from __future__ import annotations

import os

import httpx

from .models import LANGUAGE_NAMES, Artifact, ContentEvent, DeliveryPlan
from .providers import Provider, ProviderError
from .providers_openai import INSTRUCTIONS

DEFAULT_BASE_URL = "http://localhost:9337/v1"
PROBE_TIMEOUT_S = 0.4  # unreachable mesh must fail fast, not eat the budget


class MeshProvider(Provider):
    """Final translated tier in the chain: cloud first, mesh when offline."""

    def __init__(self, name: str = "mesh-local", model: str | None = None,
                 base_url: str | None = None):
        super().__init__()
        self.name = name
        self.model = model or os.environ.get("MESH_MODEL", "auto")
        self.base_url = (base_url or os.environ.get("MESH_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")

    async def render(self, plan: DeliveryPlan, event: ContentEvent) -> Artifact:
        if not (event.payload or "").strip():
            raise ProviderError("template event with no free text; cache only")
        instruction = INSTRUCTIONS[plan.modality].format(
            lang=LANGUAGE_NAMES.get(plan.language, plan.language))
        try:
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(plan.e2e_budget_ms / 1000,
                                          connect=PROBE_TIMEOUT_S)) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json={"model": self.model,
                          "messages": [{"role": "system", "content": instruction},
                                       {"role": "user", "content": event.payload}],
                          "max_tokens": 300})
        except httpx.ConnectError as exc:
            raise ProviderError("mesh unreachable") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"transport: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(f"mesh upstream {resp.status_code}")
        try:
            text = resp.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(f"mesh malformed response: {exc}") from exc
        if not text:
            raise ProviderError("mesh empty completion")
        return Artifact(plan_id=plan.id, modality=plan.modality, language=plan.language,
                        content=text, provider=self.name,
                        # Honest prior: local tier, degraded but present. The
                        # cloud QE judge is typically unreachable in the
                        # offline scenario, so this prior often stands.
                        quality_estimate=0.7)
