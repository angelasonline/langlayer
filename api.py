"""HTTP surface — the spec §2.3 endpoints the reference implementation covers.
Streams (WS ingest/delivery) are Phase 1; here events are request/response so
the whole decision->render->receipt path is inspectable."""
from __future__ import annotations

from pathlib import Path

from fastapi import (FastAPI, File, HTTPException, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .delivery import hub
from .models import (LANGUAGES, MODALITY_LABELS, Channel, ContentEvent,
                     ContextOverride, Endpoint, LanguagePref, Modality,
                     ModalityPref, PreferenceSet, PresenceSession,
                     PriorityClass, Profile, Venue, now_ms)
from .providers import default_registry
from .render import metrics, process_event
from .routing import route
from .store import Store

app = FastAPI(title="Language Layer", version="0.2")
store = Store()
registry = default_registry()

import os
import secrets

HOST_INVITE_CODE = os.environ.get("HOST_INVITE_CODE", "letmein")
COST_PER_DELIVERY_USD = 0.0009  # rough gpt-4o-mini estimate per short announcement
store.events_by_code = {}
store.attendance = []


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
    for r in receipts:
        if r.artifact_id and r.artifact_id in store.artifacts:
            store.artifacts[r.artifact_id].content = "[content not retained]"
    return {"event_id": event.id, "plans": len(receipts),
            "receipts": [r.model_dump() for r in receipts]}


class CreateEvent(BaseModel):
    name: str
    invite_code: str


@app.post("/v1/events")
def create_event(req: CreateEvent) -> dict:
    if req.invite_code != HOST_INVITE_CODE:
        raise HTTPException(403, "invalid invite code")
    venue = Venue(name=req.name)
    store.venues[venue.id] = venue
    chan = Channel(venue_id=venue.id, name="announcements")
    store.channels[chan.id] = chan
    code = "LL-" + secrets.token_hex(2).upper()
    store.events_by_code[code] = {"name": req.name, "venue_id": venue.id,
                                  "channel_id": chan.id, "created_ms": now_ms()}
    store.log("host", "event_created", code)
    return {"access_code": code, "name": req.name,
            "attendee_link": f"/join?code={code}", "host_link": f"/console?code={code}"}


def _event(code: str) -> dict:
    ev = store.events_by_code.get(code)
    if not ev:
        raise HTTPException(404, "event not found")
    return ev


@app.get("/v1/events/{code}")
def event_info(code: str) -> dict:
    ev = _event(code)
    return {"access_code": code, "name": ev["name"], "channel_id": ev["channel_id"],
            "languages": [{"tag": t, "name": n} for t, n in LANGUAGES],
            "formats": [{"kind": k, "label": v} for k, v in MODALITY_LABELS.items()
                        if k in ("captions", "simplified", "speech", "sign")]}


class JoinEvent(BaseModel):
    kind: str = "person"          # person | business
    name: str
    language: str
    modality: str = "captions"


@app.post("/v1/events/{code}/join")
def join_event(code: str, req: JoinEvent) -> dict:
    ev = _event(code)
    mod = Modality.sign if req.language == "asl" else Modality(req.modality)
    p = Profile(display_name=req.name, preferences=PreferenceSet(
        languages=[LanguagePref(tag=req.language, rank=1)],
        modalities=[ModalityPref(kind=mod, rank=1)]))
    store.profiles[p.id] = p
    e = Endpoint(profile_id=p.id, kind="mobile",
                 capabilities={"audio_out", "video_out", "text_out"})
    store.endpoints[e.id] = e
    s2 = PresenceSession(profile_id=p.id, endpoint_id=e.id,
                         attached_to=[f"venue:{ev['venue_id']}"], ttl_seconds=14400)
    store.presence[s2.id] = s2
    store.attendance.append({"event": code, "kind": req.kind, "name": req.name,
                             "language": req.language, "format": mod.value,
                             "joined_ms": now_ms()})
    return {"endpoint_id": e.id, "profile_id": p.id, "event_name": ev["name"],
            "language": req.language, "format": mod.value}


@app.get("/v1/events/{code}/attendees")
def event_attendees(code: str) -> dict:
    ev = _event(code)
    live = []
    for s2 in store.presence.values():
        if s2.expired or f"venue:{ev['venue_id']}" not in s2.attached_to:
            continue
        p = store.profiles.get(s2.profile_id)
        if p:
            langs = sorted(p.preferences.languages, key=lambda l: l.rank)
            live.append({"name": p.display_name,
                         "language": langs[0].tag if langs else "en"})
    return {"live": live, "log": [a for a in store.attendance if a["event"] == code]}


@app.get("/v1/events/{code}/summary")
def event_summary(code: str) -> dict:
    ev = _event(code)
    plan_ids = {p.id for p in store.plans.values()}
    rs = [r for r in store.receipts.values()
          if store.plans.get(r.plan_id)
          and store.events.get(store.plans[r.plan_id].event_id)
          and store.events[store.plans[r.plan_id].event_id].channel_id == ev["channel_id"]]
    delivered = [r for r in rs if r.delivered]
    langs = {store.plans[r.plan_id].language for r in delivered}
    return {"deliveries": len(rs), "delivered": len(delivered),
            "languages": sorted(langs),
            "estimated_cost_usd": round(len(delivered) * COST_PER_DELIVERY_USD, 4)}


@app.get("/v1/events/{code}/transcript")
def event_transcript(code: str) -> dict:
    ev = _event(code)
    items = []
    for evt in store.events.values():
        if evt.channel_id != ev["channel_id"]:
            continue
        rs = [r for r in store.receipts.values()
              if store.plans.get(r.plan_id) and store.plans[r.plan_id].event_id == evt.id]
        items.append({"sent_ms": evt.created_at_ms, "class": evt.priority_class.value,
                      "source_text": evt.payload or f"[template:{evt.template}]",
                      "deliveries": [{"to": (store.profiles.get(r.profile_id).display_name
                                             if store.profiles.get(r.profile_id) else "attendee"),
                                      "language": store.plans[r.plan_id].language,
                                      "format": store.plans[r.plan_id].modality.value,
                                      "delivered": r.delivered, "e2e_ms": r.e2e_ms,
                                      "sla_met": r.sla_met, "receipt_id": r.id}
                                     for r in rs]})
    items.sort(key=lambda i: i["sent_ms"])
    return {"event": ev["name"], "access_code": code, "announcements": items}


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


@app.get("/v1/attendance")
def all_attendance() -> list[dict]:
    return store.attendance[-100:]


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
@app.get("/join", response_class=HTMLResponse)
def join_page() -> str:
    return _page("join.html")


@app.get("/host", response_class=HTMLResponse)
def host_page() -> str:
    return _page("host.html")


@app.get("/console", response_class=HTMLResponse)
def console_page() -> str:
    return _page("console.html")


@app.get("/security", response_class=HTMLResponse)
def security_page() -> str:
    return _page("security.html")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> str:
    return _page("dashboard.html")


@app.post("/v1/channels/{cid}/speak")
async def speak_event(cid: str, priority_class: str = "announcement",
                      audio: UploadFile = File(...)) -> dict:
    """Push-to-talk: transcribe host speech, then fan out like a typed announcement."""
    if cid not in store.channels:
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
