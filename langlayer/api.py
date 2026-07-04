"""HTTP surface — the spec §2.3 endpoints the reference implementation covers.
Streams (WS ingest/delivery) are Phase 1; here events are request/response so
the whole decision->render->receipt path is inspectable."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .delivery import hub
from .models import (Channel, ContentEvent, ContextOverride, Endpoint,
                     LanguagePref, Modality, ModalityPref, PreferenceSet,
                     PresenceSession, PriorityClass, Profile, Venue, now_ms)
from .providers import default_registry
from .render import metrics, process_event
from .routing import route
from .store import Store

app = FastAPI(title="Language Layer — reference implementation", version="0.1")
store = Store()
registry = default_registry()


@app.post("/v1/profiles")
def create_profile(profile: Profile) -> Profile:
    store.profiles[profile.id] = profile
    return profile


@app.patch("/v1/profiles/{pid}/preferences")
def update_preferences(pid: str, prefs: dict) -> Profile:
    p = store.profiles.get(pid) or _404("profile")
    p.preferences = p.preferences.model_copy(update=prefs)
    return p


class Switch(BaseModel):
    language: str | None = None
    modality: str | None = None
    scope: str = "session"


@app.post("/v1/profiles/{pid}/switch")
def live_switch(pid: str, s: Switch) -> dict:
    p = store.profiles.get(pid) or _404("profile")
    p.session_override = {k: v for k, v in s.model_dump().items() if v and k != "scope"}
    return {"ok": True, "override": p.session_override}


@app.delete("/v1/profiles/{pid}")
def erase(pid: str) -> dict:
    store.erase_profile(pid)
    return {"erased": pid}


@app.post("/v1/endpoints")
def create_endpoint(e: Endpoint) -> Endpoint:
    store.endpoints[e.id] = e
    return e


@app.post("/v1/presence")
def open_presence(s: PresenceSession) -> PresenceSession:
    store.presence[s.id] = s
    return s


@app.post("/v1/presence/{sid}/heartbeat")
def heartbeat(sid: str, attention: str = "active") -> dict:
    s = store.presence.get(sid) or _404("presence")
    s.last_heartbeat_ms, s.attention = now_ms(), attention
    return {"ok": True, "expires_in_s": s.ttl_seconds}


@app.post("/v1/venues")
def create_venue(v: Venue) -> Venue:
    store.venues[v.id] = v
    return v


@app.post("/v1/channels")
def create_channel(c: Channel) -> Channel:
    store.channels[c.id] = c
    return c


@app.post("/v1/channels/{cid}/events")
async def ingest_event(cid: str, event: ContentEvent) -> dict:
    if cid not in store.channels:
        _404("channel")
    event.channel_id = cid
    receipts = await process_event(event, store, registry)
    # Delivery Service push: artifacts go live to any subscribed endpoint
    for r in receipts:
        plan = store.plans[r.plan_id]
        art = store.artifacts.get(r.artifact_id) if r.artifact_id else None
        profile = store.profiles.get(r.profile_id)
        await hub.push(plan.endpoint_id, {
            "type": "artifact",
            "receipt_id": r.id,
            "rider": profile.display_name if profile else r.profile_id,
            "language": plan.language, "modality": plan.modality.value,
            "content": art.content if art else None,
            "delivered": r.delivered, "source": r.source_used,
            "failovers": r.failovers, "causes": r.failover_causes,
            "e2e_ms": r.e2e_ms, "sla_met": r.sla_met, "quality": r.quality,
            "priority_class": plan.priority_class.value,
            "decisions": plan.decisions})
    return {"event_id": event.id, "plans": len(receipts),
            "receipts": [r.model_dump() for r in receipts]}


@app.websocket("/v1/deliveries/subscribe/{endpoint_id}")
async def subscribe(ws: WebSocket, endpoint_id: str) -> None:
    await hub.connect(endpoint_id, ws)
    try:
        while True:
            await ws.receive_text()  # heartbeats / acks
    except WebSocketDisconnect:
        hub.disconnect(endpoint_id)


DEMO_RIDERS = [
    ("Marisol", ["es-MX"], [Modality.speech, Modality.captions], {"audio_out", "text_out"}),
    ("Devon", ["asl", "en"], [Modality.sign, Modality.captions], {"video_out", "text_out"}),
    ("Ana", ["en"], [Modality.simplified], {"text_out"}),
]


@app.post("/v1/demo/seed")
def demo_seed() -> dict:
    """One call builds the pitch scenario: venue, channel, templates, 3 riders."""
    venue = Venue(name="Red Line — Central Platform")
    store.venues[venue.id] = venue
    chan = Channel(venue_id=venue.id, name="platform announcements")
    store.channels[chan.id] = chan
    cache = registry.get("cache")
    if cache and hasattr(cache, "preload"):
        cache.preload("arrival", {
            "es-MX": "El tren de la línea {line} llega en {min} minutos",
            "asl": "TRAIN {line} ARRIVE {min} MINUTES",
            "en": "The {line} line train arrives in {min} minutes"})
        cache.preload("evacuate", {
            "es-MX": "EMERGENCIA: evacúe la plataforma por la salida {exit}",
            "asl": "EMERGENCY EVACUATE PLATFORM EXIT {exit}",
            "en": "EMERGENCY: evacuate the platform via exit {exit}"})
    riders = []
    for name, langs, mods, caps in DEMO_RIDERS:
        p = Profile(display_name=name, preferences=PreferenceSet(
            languages=[LanguagePref(tag=t, rank=i + 1) for i, t in enumerate(langs)],
            modalities=[ModalityPref(kind=m, rank=i + 1) for i, m in enumerate(mods)]))
        store.profiles[p.id] = p
        e = Endpoint(profile_id=p.id, kind="mobile", capabilities=caps)
        store.endpoints[e.id] = e
        s2 = PresenceSession(profile_id=p.id, endpoint_id=e.id,
                             attached_to=[f"venue:{venue.id}"], ttl_seconds=86400)
        store.presence[s2.id] = s2
        riders.append({"name": name, "profile_id": p.id, "endpoint_id": e.id,
                       "language": langs[0], "modality": mods[0].value})
    return {"venue_id": venue.id, "channel_id": chan.id, "riders": riders,
            "provider_mode": "openai" if any(
                type(p).__name__ == "OpenAIProvider"
                for p in registry.providers.values()) else "simulated"}


@app.post("/v1/demo/outage/{provider_name}")
def demo_outage(provider_name: str, on: bool = True) -> dict:
    p = registry.get(provider_name) or _404("provider")
    if hasattr(p, "forced_outage"):
        p.forced_outage = on
        return {"provider": provider_name, "forced_outage": on}
    return {"provider": provider_name,
            "note": "live provider; outage injection only on simulated providers"}


@app.get("/v1/demo/riders")
def demo_riders() -> list[dict]:
    out = []
    for s2 in store.presence.values():
        if s2.expired:
            continue
        p = store.profiles.get(s2.profile_id)
        if not p:
            continue
        langs = sorted(p.preferences.languages, key=lambda l: l.rank)
        mods = sorted(p.preferences.modalities, key=lambda m: m.rank)
        out.append({"name": p.display_name, "endpoint_id": s2.endpoint_id,
                    "language": langs[0].tag if langs else "en",
                    "modality": mods[0].kind.value if mods else "captions"})
    return out


@app.get("/v1/receipts")
def list_receipts(limit: int = 25) -> list[dict]:
    items = list(store.receipts.values())[-limit:]
    out = []
    for r in reversed(items):
        plan = store.plans.get(r.plan_id)
        prof = store.profiles.get(r.profile_id)
        out.append({"id": r.id, "rider": prof.display_name if prof else r.profile_id,
                    "language": plan.language if plan else None,
                    "modality": plan.modality.value if plan else None,
                    "class": plan.priority_class.value if plan else None,
                    "delivered": r.delivered, "source": r.source_used,
                    "failovers": r.failovers, "e2e_ms": r.e2e_ms,
                    "quality": r.quality, "sla_met": r.sla_met,
                    "signature": r.signature[:12] + "…" if r.signature else ""})
    return out


def _page(name: str) -> str:
    return (Path(__file__).parent / "static" / name).read_text()


@app.get("/demo", response_class=HTMLResponse)
def demo_page() -> str:
    return _page("demo.html")


@app.get("/rider", response_class=HTMLResponse)
def rider_page() -> str:
    return _page("rider.html")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> str:
    return _page("dashboard.html")


@app.post("/v1/routes/preview")
def preview(event: ContentEvent) -> dict:
    health = {n: p.circuit.state for n, p in registry.providers.items()}
    plans = route(event, store, health)
    return {"plans": [p.model_dump() for p in plans]}


@app.get("/v1/deliveries/{rid}/receipt")
def receipt(rid: str) -> dict:
    r = store.receipts.get(rid) or _404("receipt")
    return r.model_dump()


@app.get("/v1/metrics")
def get_metrics() -> dict:
    return metrics(store)


@app.get("/v1/providers/health")
def provider_health() -> list[dict]:
    return registry.health_board()


@app.get("/v1/audit")
def audit() -> list[dict]:
    return store.audit[-200:]


def _404(what: str):
    raise HTTPException(404, f"{what} not found")
