"""Application state: single composition point for store, registry, delivery hub.

Routers read state at call time (state.store, state.registry), so tests and
operational tooling can swap implementations without touching route code.
"""
from __future__ import annotations

import os

from .delivery import DeliveryHub
from .providers import default_registry
from .store import Store

DB_URL = os.environ.get("DATABASE_URL", "sqlite:///./langlayer.db")
HOST_INVITE_CODE = os.environ.get("HOST_INVITE_CODE", "letmein")
COST_PER_DELIVERY_USD = 0.0009

store: Store = Store(db_url=DB_URL)
registry = default_registry()
hub = DeliveryHub()


def configure(db_url: str | None = None) -> None:
    """Rebuild state (used by tests and ops tooling)."""
    global store, registry
    store = Store(db_url=db_url)
    registry = default_registry()
