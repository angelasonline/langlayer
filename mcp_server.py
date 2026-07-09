"""Language Layer MCP server — makes the delivery layer operable by agents.

Exposes Language Layer's host operations as Model Context Protocol tools, so
Goose, Claude, or any MCP-capable agent can run a space: create it, send
announcements, and read the signed delivery receipts that prove access was
provided. This is the integration seam for agent ecosystems (Goose/GDK):
Language Layer stays delivery infrastructure; agents orchestrate it.

Run (stdio, the transport Goose and Claude Desktop expect):

    pip install -r requirements-mcp.txt
    LL_BASE_URL=https://langlayer.onrender.com \
    LL_INVITE_CODE=<host invite code> \
    python mcp_server.py

Wire-up snippets for Goose and Claude Desktop: see MCP-AGENTS.md.
Design notes:
- Tools wrap the public HTTP API; no engine imports, so this file works
  against any deployment (cloud, venue box, laptop) via LL_BASE_URL.
- The invite code stays in the agent host's environment, never in chat.
- Read tools are safe for any agent; send_announcement is the only tool
  with real-world effect, and its docstring says so for the agent's benefit.
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.environ.get("LL_BASE_URL", "http://localhost:8000").rstrip("/")
INVITE_CODE = os.environ.get("LL_INVITE_CODE", "")

mcp = FastMCP(
    "language-layer",
    instructions=(
        "Tools for operating a Language Layer deployment: create spaces, send "
        "announcements that reach every attendee in their own language and "
        "format, and read signed delivery receipts. Announcements have "
        "real-world effect on connected attendees; confirm intent before "
        "sending. Emergency-class sends use pre-translated templates by "
        "policy and never wait on a model."))


async def _get(path: str, **params: Any) -> Any:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{BASE_URL}{path}", params=params or None)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, payload: dict) -> Any:
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(f"{BASE_URL}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()


@mcp.tool()
async def get_health() -> dict:
    """Deployment health: ok flag, storage durability, database engine, and
    space count. Also triggers the provider warm-up so a following send is
    fast."""
    health = await _get("/healthz")
    try:
        health["warm"] = await _get("/v1/warm")
    except httpx.HTTPError:
        health["warm"] = "warm endpoint unavailable"
    return health


@mcp.tool()
async def get_coverage() -> dict:
    """The public capability map: every supported language with its text,
    voice, and model-quality flags, plus the honest notes on sign language
    and device-voice gaps."""
    return await _get("/v1/coverage")


@mcp.tool()
async def create_space(name: str) -> dict:
    """Create a new space (venue/event). Returns the access code, attendee
    link, and host console link. Requires LL_INVITE_CODE in the server
    environment."""
    if not INVITE_CODE:
        return {"error": "LL_INVITE_CODE is not set in the MCP server environment"}
    return await _post("/v1/events", {"name": name, "invite_code": INVITE_CODE})


@mcp.tool()
async def get_space(code: str) -> dict:
    """Space info by access code: name, channel id, and the language list
    attendees can choose from."""
    return await _get(f"/v1/events/{code}")


@mcp.tool()
async def get_attendees(code: str) -> Any:
    """Who has joined the space: name, type, language, format, join time."""
    return await _get(f"/v1/events/{code}/attendees")


@mcp.tool()
async def send_announcement(code: str, text: str,
                            priority_class: str = "announcement") -> dict:
    """Send an announcement to every attendee of the space, each in their own
    language and format. REAL-WORLD EFFECT: connected attendees receive this
    immediately. priority_class is one of: announcement, conversational,
    live, emergency. The emergency class delivers pre-translated templates
    first by policy and marks free text with an honest untranslated notice
    if no source can translate in time."""
    info = await _get(f"/v1/events/{code}")
    result = await _post(f"/v1/channels/{info['channel_id']}/events",
                         {"channel_id": info["channel_id"],
                          "priority_class": priority_class,
                          "kind": "text", "payload": text})
    receipts = result.get("receipts", [])
    delivered = sum(1 for r in receipts if r.get("delivered"))
    return {"sent": text, "priority_class": priority_class,
            "delivered": delivered, "recipients": len(receipts),
            "sla_missed": sum(1 for r in receipts if not r.get("sla_met")),
            "receipts": receipts}


@mcp.tool()
async def get_summary(code: str) -> dict:
    """Space delivery summary: connected count, languages present, delivery
    totals, and estimated cost."""
    return await _get(f"/v1/events/{code}/summary")


@mcp.tool()
async def get_transcript(code: str) -> dict:
    """The space's exportable transcript: every announcement sent and its
    per-recipient delivery results. This is the access documentation record."""
    return await _get(f"/v1/events/{code}/transcript")


if __name__ == "__main__":
    mcp.run()
