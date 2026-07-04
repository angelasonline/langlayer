"""Delivery Service (spec §1.5(6)) — WebSocket endpoint adapter.

Endpoints subscribe once; the layer pushes artifacts addressed to them the
moment a receipt is cut. Venue-system, WebRTC, and caption-embed adapters
implement this same push interface in Phase 1+.
"""
from __future__ import annotations

from typing import Any

from fastapi import WebSocket


class DeliveryHub:
    def __init__(self) -> None:
        self.sockets: dict[str, WebSocket] = {}

    async def connect(self, endpoint_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self.sockets[endpoint_id] = ws

    def disconnect(self, endpoint_id: str) -> None:
        self.sockets.pop(endpoint_id, None)

    async def push(self, endpoint_id: str, payload: dict[str, Any]) -> bool:
        ws = self.sockets.get(endpoint_id)
        if ws is None:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.disconnect(endpoint_id)
            return False


hub = DeliveryHub()
