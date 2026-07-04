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


import secrets


class CreateEvent(BaseModel):
    name: str
    invite_code: str


class JoinEvent(BaseModel):
    kind: str = "person"          # person | business
    name: str
    language: str
    modality: str = "captions"


def _event(code: str) -> dict:
    ev = state.store.events_by_code.get(code)
    if not ev:
        raise HTTPException(404, "space not found")
    return ev


@router.post("/v1/events")
def create_event(req: CreateEvent, request: Request,
                 _rl=Depends(limiter("space_create"))) -> dict:
    if req.invite_code != state.HOST_INVITE_CODE:
        raise HTTPException(403, "invalid invite code")
    venue = Venue(name=req.name)
    chan = Channel(venue_id=venue.id, name="announcements")
    code = "LL-" + secrets.token_hex(2).upper()
    space = {"name": req.name, "venue_id": venue.id,
             "channel_id": chan.id, "created_ms": now_ms()}
    state.store.save_space(code, space, venue, chan)
    state.store.log("host", "space_created", code)
    return {"access_code": code, "name": req.name,
            "attendee_link": f"/join?code={code}", "host_link": f"/console?code={code}"}


@router.get("/v1/events/{code}")
def event_info(code: str) -> dict:
    ev = _event(code)
    from ..models import LANGUAGE_INFO
    return {"access_code": code, "name": ev["name"], "channel_id": ev["channel_id"],
            "languages": [LANGUAGE_INFO[t] for t, _ in LANGUAGES],
            "formats": [{"kind": k, "label": v} for k, v in MODALITY_LABELS.items()
                        if k in ("captions", "simplified", "speech", "sign")]}


@router.post("/v1/events/{code}/join")
def join_event(code: str, req: JoinEvent, request: Request,
               _rl=Depends(limiter("join"))) -> dict:
    ev = _event(code)
    mod = Modality.sign if req.language == "asl" else Modality(req.modality)
    p = Profile(display_name=req.name, preferences=PreferenceSet(
        languages=[LanguagePref(tag=req.language, rank=1)],
        modalities=[ModalityPref(kind=mod, rank=1)]))
    e = Endpoint(profile_id=p.id, kind="mobile",
                 capabilities={"audio_out", "video_out", "text_out"})
    s2 = PresenceSession(profile_id=p.id, endpoint_id=e.id,
                         attached_to=[f"venue:{ev['venue_id']}"], ttl_seconds=14400)
    state.store.presence[s2.id] = s2
    state.store.save_join(p, e, [f"venue:{ev['venue_id']}"],
                    {"event": code, "kind": req.kind, "name": req.name,
                     "language": req.language, "format": mod.value,
                     "joined_ms": now_ms()})
    return {"endpoint_id": e.id, "profile_id": p.id, "event_name": ev["name"],
            "language": req.language, "format": mod.value}


@router.get("/v1/events/{code}/attendees")
def event_attendees(code: str) -> dict:
    ev = _event(code)
    live = []
    for s2 in state.store.presence.values():
        if s2.expired or f"venue:{ev['venue_id']}" not in s2.attached_to:
            continue
        p = state.store.profiles.get(s2.profile_id)
        if p:
            langs = sorted(p.preferences.languages, key=lambda l: l.rank)
            live.append({"name": p.display_name,
                         "language": langs[0].tag if langs else "en"})
    return {"live": live, "log": [a for a in state.store.attendance if a["event"] == code]}


@router.get("/v1/events/{code}/summary")
def event_summary(code: str) -> dict:
    ev = _event(code)
    plan_ids = {p.id for p in state.store.plans.values()}
    rs = [r for r in state.store.receipts.values()
          if state.store.plans.get(r.plan_id)
          and state.store.events.get(state.store.plans[r.plan_id].event_id)
          and state.store.events[state.store.plans[r.plan_id].event_id].channel_id == ev["channel_id"]]
    delivered = [r for r in rs if r.delivered]
    langs = {state.store.plans[r.plan_id].language for r in delivered}
    return {"deliveries": len(rs), "delivered": len(delivered),
            "languages": sorted(langs),
            "estimated_cost_usd": round(len(delivered) * state.COST_PER_DELIVERY_USD, 4)}


@router.get("/v1/events/{code}/transcript")
def event_transcript(code: str) -> dict:
    ev = _event(code)
    items = []
    for evt in state.store.events.values():
        if evt.channel_id != ev["channel_id"]:
            continue
        rs = [r for r in state.store.receipts.values()
              if state.store.plans.get(r.plan_id) and state.store.plans[r.plan_id].event_id == evt.id]
        items.append({"sent_ms": evt.created_at_ms, "class": evt.priority_class.value,
                      "source_text": evt.payload or f"[template:{evt.template}]",
                      "deliveries": [{"to": (state.store.profiles.get(r.profile_id).display_name
                                             if state.store.profiles.get(r.profile_id) else "attendee"),
                                      "language": state.store.plans[r.plan_id].language,
                                      "format": state.store.plans[r.plan_id].modality.value,
                                      "delivered": r.delivered, "e2e_ms": r.e2e_ms,
                                      "sla_met": r.sla_met, "receipt_id": r.id}
                                     for r in rs]})
    items.sort(key=lambda i: i["sent_ms"])
    return {"event": ev["name"], "access_code": code, "announcements": items}


@router.get("/v1/attendance")
def all_attendance() -> list[dict]:
    return state.store.attendance[-100:]
