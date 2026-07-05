"""Language Layer application: composition root.

Routers live in langlayer/routers/. Shared state lives in langlayer/state.py.
This module stays import-compatible: `from langlayer.api import app`.
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from . import state
from .routers import core, pages, spaces

if os.environ.get("SENTRY_DSN"):
    try:
        import sentry_sdk
        sentry_sdk.init(dsn=os.environ["SENTRY_DSN"], traces_sample_rate=0.1)
    except ImportError:
        pass

app = FastAPI(title="Language Layer", version="1.0")
app.include_router(pages.router)
app.include_router(spaces.router)
app.include_router(core.router)


def __getattr__(name):
    # Back-compat: langlayer.api.store / .registry keep working for tools/tests
    if name in ("store", "registry", "hub"):
        return getattr(state, name)
    raise AttributeError(name)

@app.get("/v1/warm")
async def warm() -> dict:
    """Wake the database and both AI providers so first real message is fast."""
    import asyncio as _a
    results = {"db": False, "primary": False, "alt": False}
    try:
        _ = len(state.store.venues); results["db"] = True
    except Exception:
        pass
    async def ping(name, key):
        p = state.registry.get(name)
        if p is None or not getattr(p, "api_key", None):
            return
        try:
            from .models import ContentEvent, DeliveryPlan, Modality, PriorityClass, SourceStep
            ev = ContentEvent(channel_id="warm", priority_class=PriorityClass.announcement, payload="hello")
            plan = DeliveryPlan(profile_id="warm", endpoint_id="warm", language="es",
                                modality=Modality.captions, priority_class=PriorityClass.announcement,
                                source_chain=[SourceStep(provider=name)], ttfo_budget_ms=4000,
                                e2e_budget_ms=8000, reasons=[])
            await _a.wait_for(p.render(plan, ev), timeout=8)
            results[key] = True
        except Exception:
            pass
    await _a.gather(ping("ai-realtime", "primary"), ping("ai-realtime-alt", "alt"))
    return results

# ---- Keep-alive: the system never sleeps between demos ----
import asyncio as _aio
import contextlib as _ctx

_KEEPALIVE_MINUTES = 10

async def _keepalive_loop():
    # First warm immediately on boot, then every N minutes.
    while True:
        with _ctx.suppress(Exception):
            await warm()
        await _aio.sleep(_KEEPALIVE_MINUTES * 60)

@app.on_event("startup")
async def _start_keepalive():
    _aio.create_task(_keepalive_loop())

