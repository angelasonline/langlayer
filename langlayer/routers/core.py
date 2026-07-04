"""Auto-split router: reads shared state at call time."""
from __future__ import annotations

import os

from fastapi import (APIRouter, Depends, File, HTTPException, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel

from .. import state
from ..ratelimit import limiter
from ..models import (LANGUAGES, MODALITY_LABELS, Channel, ContentEvent,
                      ContextOverride, Endpoint, LanguagePref, Modality,
                      ModalityPref, PreferenceSet, PresenceSession,
                      PriorityClass, Profile, Venue, now_ms)
from ..render import metrics, process_event
from ..routing import route

router = APIRouter()


def _404(what: str):
    raise HTTPException(404, f"{what} not found")


def _page(name: str) -> str:
    return (Path(__file__).parent.parent / "static" / name).read_text()


class Switch(BaseModel):
    language: str | None = None
    modality: str | None = None
    scope: str = "session"


DEMO_RIDERS = [
    ("Monica", ["es"], [Modality.speech, Modality.captions], {"audio_out", "text_out"}),
    ("Sandi", ["asl", "en"], [Modality.sign, Modality.captions], {"video_out", "text_out"}),
    ("Nate", ["zh", "en"], [Modality.simplified, Modality.captions], {"text_out"}),
]


@router.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "durable": state.store.engine is not None,
            "db": state.store.engine.dialect.name if state.store.engine else "memory",
            "spaces": len(state.store.events_by_code)}

import os


@router.post("/v1/profiles")
def create_profile(profile: Profile) -> Profile:
    state.store.profiles[profile.id] = profile
    return profile


@router.patch("/v1/profiles/{pid}/preferences")
def update_preferences(pid: str, prefs: dict) -> Profile:
    p = state.store.profiles.get(pid) or _404("profile")
    p.preferences = p.preferences.model_copy(update=prefs)
    return p


@router.post("/v1/profiles/{pid}/switch")
def live_switch(pid: str, s: Switch) -> dict:
    p = state.store.profiles.get(pid) or _404("profile")
    p.session_override = {k: v for k, v in s.model_dump().items() if v and k != "scope"}
    return {"ok": True, "override": p.session_override}


@router.delete("/v1/profiles/{pid}")
def erase(pid: str) -> dict:
    state.store.erase_profile(pid)
    return {"erased": pid}


@router.post("/v1/endpoints")
def create_endpoint(e: Endpoint) -> Endpoint:
    state.store.endpoints[e.id] = e
    return e


@router.post("/v1/presence")
def open_presence(s: PresenceSession) -> PresenceSession:
    state.store.presence[s.id] = s
    return s


@router.post("/v1/presence/{sid}/heartbeat")
def heartbeat(sid: str, attention: str = "active") -> dict:
    s = state.store.presence.get(sid) or _404("presence")
    s.last_heartbeat_ms, s.attention = now_ms(), attention
    return {"ok": True, "expires_in_s": s.ttl_seconds}


@router.post("/v1/venues")
def create_venue(v: Venue) -> Venue:
    state.store.venues[v.id] = v
    return v


@router.post("/v1/channels")
def create_channel(c: Channel) -> Channel:
    state.store.channels[c.id] = c
    return c


@router.post("/v1/channels/{cid}/events")
async def ingest_event(cid: str, event: ContentEvent, request: Request = None) -> dict:
    if request is not None:
        from ..ratelimit import check
        check(request, "ingest")
    if cid not in state.store.channels:
        _404("channel")
    event.channel_id = cid
    receipts = await process_event(event, state.store, state.registry)
    # Delivery Service push: artifacts go live to any subscribed endpoint
    for r in receipts:
        plan = state.store.plans[r.plan_id]
        art = state.store.artifacts.get(r.artifact_id) if r.artifact_id else None
        profile = state.store.profiles.get(r.profile_id)
        await state.hub.push(plan.endpoint_id, {
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
    for r in receipts:
        if r.artifact_id and r.artifact_id in state.store.artifacts:
            state.store.artifacts[r.artifact_id].content = "[content not retained]"
    return {"event_id": event.id, "plans": len(receipts),
            "receipts": [r.model_dump() for r in receipts]}


@router.websocket("/v1/deliveries/subscribe/{endpoint_id}")
async def subscribe(ws: WebSocket, endpoint_id: str) -> None:
    await state.hub.connect(endpoint_id, ws)
    state.store.revive_presence(endpoint_id)  # reconnect after restart = still present
    try:
        while True:
            await ws.receive_text()  # heartbeats / acks
    except WebSocketDisconnect:
        state.hub.disconnect(endpoint_id)


@router.post("/v1/demo/seed")
def demo_seed() -> dict:
    """One call builds the pitch scenario: venue, channel, templates, 3 riders."""
    venue = Venue(name="Red Line — Central Platform")
    state.store.venues[venue.id] = venue
    chan = Channel(venue_id=venue.id, name="platform announcements")
    state.store.channels[chan.id] = chan
    cache = state.registry.get("cache")
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
        state.store.profiles[p.id] = p
        e = Endpoint(profile_id=p.id, kind="mobile", capabilities=caps)
        state.store.endpoints[e.id] = e
        s2 = PresenceSession(profile_id=p.id, endpoint_id=e.id,
                             attached_to=[f"venue:{venue.id}"], ttl_seconds=86400)
        state.store.presence[s2.id] = s2
        riders.append({"name": name, "profile_id": p.id, "endpoint_id": e.id,
                       "language": langs[0], "modality": mods[0].value})
    return {"venue_id": venue.id, "channel_id": chan.id, "riders": riders,
            "provider_mode": (
                "claude-fable-5" if any(type(p).__name__ == "AnthropicProvider"
                                         for p in state.registry.providers.values())
                else "openai" if any(type(p).__name__ == "OpenAIProvider"
                                     for p in state.registry.providers.values())
                else "simulated")}


@router.post("/v1/demo/outage/{provider_name}")
def demo_outage(provider_name: str, on: bool = True) -> dict:
    p = state.registry.get(provider_name) or _404("provider")
    if hasattr(p, "forced_outage"):
        p.forced_outage = on
        return {"provider": provider_name, "forced_outage": on}
    return {"provider": provider_name,
            "note": "live provider; outage injection only on simulated providers"}


@router.get("/v1/demo/riders")
def demo_riders() -> list[dict]:
    out = []
    for s2 in state.store.presence.values():
        if s2.expired:
            continue
        p = state.store.profiles.get(s2.profile_id)
        if not p:
            continue
        langs = sorted(p.preferences.languages, key=lambda l: l.rank)
        mods = sorted(p.preferences.modalities, key=lambda m: m.rank)
        out.append({"name": p.display_name, "endpoint_id": s2.endpoint_id,
                    "language": langs[0].tag if langs else "en",
                    "modality": mods[0].kind.value if mods else "captions"})
    return out


@router.get("/v1/receipts")
def list_receipts(limit: int = 25) -> list[dict]:
    items = list(state.store.receipts.values())[-limit:]
    out = []
    for r in reversed(items):
        plan = state.store.plans.get(r.plan_id)
        prof = state.store.profiles.get(r.profile_id)
        out.append({"id": r.id, "rider": prof.display_name if prof else r.profile_id,
                    "language": plan.language if plan else None,
                    "modality": plan.modality.value if plan else None,
                    "class": plan.priority_class.value if plan else None,
                    "delivered": r.delivered, "source": r.source_used,
                    "failovers": r.failovers, "e2e_ms": r.e2e_ms,
                    "quality": r.quality, "sla_met": r.sla_met,
                    "signature": r.signature[:12] + "…" if r.signature else ""})
    return out


@router.get("/v1/coverage")
def coverage() -> dict:
    from ..models import LANGUAGES_FULL
    return {"languages": [{"tag": t, "name": n, "rtl": r, "voice": v, "tier": tier}
                          for t, n, r, v, tier in LANGUAGES_FULL],
            "notes": {
                "text": "All listed languages: text translation available, quality scored per delivery.",
                "voice": "Device speech voices do not exist for every language. Where missing, spoken audio falls back to captions.",
                "sign": "No frontier model can produce sign-language video today. ASL is delivered as text gloss; video is being developed with Deaf-led evaluation."}}


@router.post("/v1/channels/{cid}/speak")
async def speak_event(cid: str, request: Request, priority_class: str = "announcement",
                      audio: UploadFile = File(...),
                      _rl=Depends(limiter("speak"))) -> dict:
    """Push-to-talk: transcribe host speech, then fan out like a typed announcement."""
    if cid not in state.store.channels:
        _404("channel")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise HTTPException(400, "Voice input requires live mode (OPENAI_API_KEY not set)")
    data = await audio.read()
    if len(data) < 200:
        raise HTTPException(400, "No audio captured. Hold the button while speaking.")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": (audio.filename or "speech.webm", data,
                                 audio.content_type or "audio/webm")},
                data={"model": "whisper-1"})
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Transcription failed: {exc}")
    if resp.status_code != 200:
        raise HTTPException(502, f"Transcription failed ({resp.status_code})")
    text = resp.json().get("text", "").strip()
    if not text:
        raise HTTPException(400, "Could not hear anything in that recording.")
    event = ContentEvent(channel_id=cid, priority_class=PriorityClass(priority_class),
                         payload=text)
    result = await ingest_event(cid, event)
    result["transcript"] = text
    return result


@router.post("/v1/routes/preview")
def preview(event: ContentEvent) -> dict:
    health = {n: p.circuit.state for n, p in state.registry.providers.items()}
    plans = route(event, state.store, health)
    return {"plans": [p.model_dump() for p in plans]}


@router.get("/v1/deliveries/{rid}/receipt")
def receipt(rid: str) -> dict:
    r = state.store.receipts.get(rid) or _404("receipt")
    return r.model_dump()


@router.get("/v1/metrics")
def get_metrics() -> dict:
    return metrics(state.store)


@router.get("/v1/providers/health")
def provider_health() -> list[dict]:
    return state.registry.health_board()


@router.get("/v1/audit")
def audit() -> list[dict]:
    return state.store.audit[-200:]
