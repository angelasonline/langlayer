"""Auto-split router: reads shared state at call time."""
from __future__ import annotations

import os

from fastapi import (APIRouter, File, HTTPException, UploadFile, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import HTMLResponse
from pathlib import Path
from pydantic import BaseModel

from .. import state
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


@router.get("/demo", response_class=HTMLResponse)
def demo_page() -> str:
    return _page("demo.html")


@router.get("/rider", response_class=HTMLResponse)


@router.get("/join", response_class=HTMLResponse)
def join_page() -> str:
    return _page("join.html")


@router.get("/host", response_class=HTMLResponse)
def host_page() -> str:
    return _page("host.html")


@router.get("/console", response_class=HTMLResponse)
def console_page() -> str:
    return _page("console.html")


@router.get("/", response_class=HTMLResponse)
def home_page() -> str:
    return _page("index.html")


@router.get("/coverage", response_class=HTMLResponse)
def coverage_page() -> str:
    return _page("coverage.html")


@router.get("/security", response_class=HTMLResponse)
def security_page() -> str:
    return _page("security.html")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page() -> str:
    return _page("dashboard.html")
