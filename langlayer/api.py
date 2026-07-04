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
