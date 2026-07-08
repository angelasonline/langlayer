"""Render Plane provider adapters — spec §1.5(5), §2.7.

One contract wraps every source: AI realtime, AI batch, cache/templates,
human bridge. The real OpenAI Realtime adapter implements this same
interface; the engine never knows the difference.
"""
from __future__ import annotations

import os

import asyncio
import time
from collections import deque

from .models import Artifact, ContentEvent, DeliveryPlan, Modality


class ProviderError(Exception):
    pass


class CacheMiss(ProviderError):
    """Expected condition, not provider unhealth: must NOT trip the circuit."""


class CircuitBreaker:
    """3 failures in a 60s window opens the circuit; half-opens after 15s."""

    def __init__(self, threshold: int = 3, window_s: int = 60, cooldown_s: int = 15):
        self.threshold, self.window_s, self.cooldown_s = threshold, window_s, cooldown_s
        self.failures: deque[float] = deque()
        self.opened_at: float | None = None

    def record_failure(self) -> None:
        t = time.monotonic()
        self.failures.append(t)
        while self.failures and t - self.failures[0] > self.window_s:
            self.failures.popleft()
        if len(self.failures) >= self.threshold:
            self.opened_at = t

    def record_success(self) -> None:
        self.failures.clear()
        self.opened_at = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        if time.monotonic() - self.opened_at > self.cooldown_s:
            return "half_open"
        return "open"


class Provider:
    """Adapter contract: render(plan, event) -> Artifact, streaming-first."""

    name = "base"

    def __init__(self) -> None:
        self.circuit = CircuitBreaker()
        self.latencies_ms: deque[int] = deque(maxlen=200)

    async def render(self, plan: DeliveryPlan, event: ContentEvent) -> Artifact:
        raise NotImplementedError

    def health(self) -> dict:
        lat = sorted(self.latencies_ms)
        p95 = lat[int(len(lat) * 0.95) - 1] if lat else None
        return {"provider": self.name, "circuit": self.circuit.state, "p95_ttfo_ms": p95}


def _transform(text: str, language: str, modality: Modality) -> str:
    """Simulated rendering. The real adapter calls the model here."""
    if modality == Modality.sign:
        return f"[sign:{language} video] {text}"
    if modality == Modality.speech:
        return f"[tts:{language} audio] {text}"
    if modality == Modality.simplified:
        return f"[{language} simplified] {text}"
    return f"[{language}] {text}"


class SimulatedAIRealtime(Provider):
    """Stands in for the OpenAI Realtime adapter. Latency & outage injectable."""

    def __init__(self, name: str = "ai-realtime", base_latency_ms: int = 180,
                 quality: float = 0.93):
        super().__init__()
        self.name = name
        self.base_latency_ms = base_latency_ms
        self.quality = quality
        self.forced_outage = False

    async def render(self, plan: DeliveryPlan, event: ContentEvent) -> Artifact:
        if self.forced_outage:
            raise ProviderError("upstream unavailable")
        await asyncio.sleep(self.base_latency_ms / 1000)
        return Artifact(plan_id=plan.id, modality=plan.modality, language=plan.language,
                        content=_transform(event.payload, plan.language, plan.modality),
                        provider=self.name, quality_estimate=self.quality)


class CacheProvider(Provider):
    """Templates and pre-rendered artifacts. Deterministic, ~instant.

    Emergency and announcement classes hit this FIRST by design (W5/W6):
    an evacuation alert must never wait on a model or hallucinate.
    """

    name = "cache"

    def __init__(self) -> None:
        super().__init__()
        # templates[template][language] -> string with {slots}
        self.templates: dict[str, dict[str, str]] = {}

    def preload(self, template: str, translations: dict[str, str]) -> None:
        self.templates.setdefault(template, {}).update(translations)

    async def render(self, plan: DeliveryPlan, event: ContentEvent) -> Artifact:
        if not event.template or plan.language not in self.templates.get(event.template, {}):
            raise CacheMiss("cache miss")
        text = self.templates[event.template][plan.language].format(**event.slots)
        return Artifact(plan_id=plan.id, modality=plan.modality, language=plan.language,
                        content=_transform(text, plan.language, plan.modality),
                        provider=self.name, quality_estimate=0.99)


class HumanBridgeSim(Provider):
    """Simulated interpreter bridge — slower, highest quality (W8)."""

    name = "human-bridge"

    async def render(self, plan: DeliveryPlan, event: ContentEvent) -> Artifact:
        await asyncio.sleep(0.35)
        return Artifact(plan_id=plan.id, modality=plan.modality, language=plan.language,
                        content="[Human interpreter dispatch: architecture complete; "
                                "live interpreter network integration pending] " + event.payload,
                        provider=self.name, quality_estimate=1.0)


class PAPassthrough(Provider):
    """Last-resort emergency fallback: source-language passthrough with notice."""

    name = "pa-passthrough"

    async def render(self, plan: DeliveryPlan, event: ContentEvent) -> Artifact:
        return Artifact(plan_id=plan.id, modality=plan.modality, language=event.source_language,
                        content=f"[untranslated notice] {event.payload}",
                        provider=self.name, quality_estimate=0.3)


class ProviderRegistry:
    def __init__(self) -> None:
        self.providers: dict[str, Provider] = {}

    def register(self, p: Provider) -> Provider:
        self.providers[p.name] = p
        return p

    def get(self, name: str) -> Provider | None:
        return self.providers.get(name)

    def health_board(self) -> list[dict]:
        return [p.health() for p in self.providers.values()]


DEMO_TEMPLATES = {
    "arrival": {
        "es-MX": "El tren de la l\u00ednea {line} llega en {min} minutos",
        "es": "El tren de la l\u00ednea {line} llega en {min} minutos",
        "asl": "TRAIN {line} ARRIVE {min} MINUTES",
        "zh": "{line}\u53f7\u7ebf\u5217\u8f66\u5c06\u5728{min}\u5206\u949f\u540e\u5230\u8fbe",
        "en": "The {line} line train arrives in {min} minutes"},
    "evacuate": {
        "es-MX": "EMERGENCIA: evac\u00fae la plataforma por la salida {exit}",
        "es": "EMERGENCIA: evac\u00fae la plataforma por la salida {exit}",
        "asl": "EMERGENCY EVACUATE PLATFORM EXIT {exit}",
        "zh": "\u7d27\u6025\u60c5\u51b5\uff1a\u8bf7\u4ece{exit}\u51fa\u53e3\u64a4\u79bb\u7ad9\u53f0",
        "en": "EMERGENCY: evacuate the platform via exit {exit}"},
}


def default_registry() -> ProviderRegistry:
    import os
    r = ProviderRegistry()
    has_oai = bool(os.environ.get("OPENAI_API_KEY"))
    has_ant = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if has_ant:
        from .providers_anthropic import AnthropicProvider
        r.register(AnthropicProvider("ai-realtime", model="claude-sonnet-4-6"))
        if has_oai:
            from .providers_openai import OpenAIProvider
            r.register(OpenAIProvider("ai-realtime-alt", model="gpt-4o-mini"))
        else:
            r.register(AnthropicProvider("ai-realtime-alt", model="claude-sonnet-4-6"))
    elif has_oai:
        from .providers_openai import OpenAIProvider
        r.register(OpenAIProvider("ai-realtime", model="gpt-4o-mini"))
        r.register(OpenAIProvider("ai-realtime-alt", model="gpt-4o"))
    else:
        r.register(SimulatedAIRealtime("ai-realtime", base_latency_ms=180, quality=0.93))
        r.register(SimulatedAIRealtime("ai-realtime-alt", base_latency_ms=260, quality=0.90))
    r.register(SimulatedAIRealtime("ai-batch", base_latency_ms=250, quality=0.95))
    cache = CacheProvider()
    for tpl, translations in DEMO_TEMPLATES.items():
        cache.preload(tpl, translations)
    r.register(cache)
    if os.environ.get("MESH_BASE_URL") or os.environ.get("MESH_ENABLED"):
        from .providers_mesh import MeshProvider
        r.register(MeshProvider())
    r.register(HumanBridgeSim())
    r.register(PAPassthrough())
    return r
