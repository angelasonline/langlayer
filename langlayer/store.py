"""Storage.

Two modes, same interface:
- Store()            pure in-memory (tests, CLI demo)
- Store(db_url=...)  write-through durability: memory is the hot cache, the
  database (SQLite or Postgres via DATABASE_URL) is the source of truth.
  Spaces, profiles, endpoints, attachments, content events, receipts, and
  attendance survive restarts and redeploys. Presence stays ephemeral by
  design (spec 1.6) and is revived when an endpoint reconnects.
"""
from __future__ import annotations

import json
from typing import Optional

import sqlalchemy as sa

from .models import (Artifact, Channel, ContentEvent, DeliveryPlan,
                     DeliveryReceipt, Endpoint, PresenceSession, Profile, Venue)

_META = sa.MetaData()
_KV = sa.Table("kv", _META,
               sa.Column("kind", sa.String(24), primary_key=True),
               sa.Column("id", sa.String(64), primary_key=True),
               sa.Column("data", sa.Text, nullable=False))
_ATT = sa.Table("attendance", _META,
                sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
                sa.Column("data", sa.Text, nullable=False))


def _normalize(url: str) -> str:
    # Render/Heroku style postgres:// -> SQLAlchemy psycopg driver
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://") and "+psycopg" not in url:
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


class Store:
    def __init__(self, db_url: Optional[str] = None) -> None:
        self.profiles: dict[str, Profile] = {}
        self.endpoints: dict[str, Endpoint] = {}
        self.presence: dict[str, PresenceSession] = {}
        self.venues: dict[str, Venue] = {}
        self.channels: dict[str, Channel] = {}
        self.events: dict[str, ContentEvent] = {}
        self.plans: dict[str, DeliveryPlan] = {}
        self.artifacts: dict[str, Artifact] = {}
        self.receipts: dict[str, DeliveryReceipt] = {}
        self.audit: list[dict] = []
        self.events_by_code: dict[str, dict] = {}
        self.attendance: list[dict] = []
        self.attachments: dict[str, dict] = {}   # endpoint_id -> {profile_id, attached_to}

        self.engine = None
        if db_url:
            self.engine = sa.create_engine(_normalize(db_url), pool_pre_ping=True,
                                           pool_size=5, max_overflow=5)
            _META.create_all(self.engine)
            self._migrate()
            self._load()

    # ---------- schema migrations ----------
    # Lightweight versioned migrations; move to Alembic at the first
    # backwards-incompatible schema change.
    SCHEMA_VERSION = 1
    MIGRATIONS: dict[int, str] = {
        # 2: "ALTER TABLE ...",
    }

    def _migrate(self) -> None:
        with self.engine.begin() as cx:
            cx.execute(sa.text(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"))
            row = cx.execute(sa.text("SELECT version FROM schema_version")).fetchone()
            current = row[0] if row else 0
            if row is None:
                cx.execute(sa.text("INSERT INTO schema_version (version) VALUES (:v)"),
                           {"v": self.SCHEMA_VERSION})
                current = self.SCHEMA_VERSION
            for v in range(current + 1, self.SCHEMA_VERSION + 1):
                if v in self.MIGRATIONS:
                    cx.execute(sa.text(self.MIGRATIONS[v]))
                cx.execute(sa.text("UPDATE schema_version SET version = :v"), {"v": v})

    # ---------- durability primitives ----------
    def _upsert_stmt(self, kind: str, id_: str, data: dict):
        if self.engine.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _ins
        else:
            from sqlalchemy.dialects.sqlite import insert as _ins
        stmt = _ins(_KV).values(kind=kind, id=id_, data=json.dumps(data, default=str))
        return stmt.on_conflict_do_update(index_elements=["kind", "id"],
                                          set_={"data": stmt.excluded.data})

    def _put(self, kind: str, id_: str, data: dict) -> None:
        if not self.engine:
            return
        with self.engine.begin() as cx:
            cx.execute(self._upsert_stmt(kind, id_, data))

    def _put_many(self, rows: list) -> None:
        if not self.engine or not rows:
            return
        with self.engine.begin() as cx:
            for kind, id_, data in rows:
                cx.execute(self._upsert_stmt(kind, id_, data))

    def _delete_kv(self, kind: str, id_: str) -> None:
        if not self.engine:
            return
        with self.engine.begin() as cx:
            cx.execute(sa.delete(_KV).where(_KV.c.kind == kind, _KV.c.id == id_))

    def _load(self) -> None:
        with self.engine.connect() as cx:
            for kind, id_, raw in cx.execute(sa.select(_KV.c.kind, _KV.c.id, _KV.c.data)):
                data = json.loads(raw)
                if kind == "space":
                    self.events_by_code[id_] = data
                elif kind == "venue":
                    self.venues[id_] = Venue(**data)
                elif kind == "channel":
                    self.channels[id_] = Channel(**data)
                elif kind == "profile":
                    self.profiles[id_] = Profile(**data)
                elif kind == "endpoint":
                    data["capabilities"] = set(data.get("capabilities", []))
                    self.endpoints[id_] = Endpoint(**data)
                elif kind == "attach":
                    self.attachments[id_] = data
                elif kind == "cevent":
                    self.events[id_] = ContentEvent(**data)
                elif kind == "plan":
                    self.plans[id_] = DeliveryPlan(**data)
                elif kind == "receipt":
                    self.receipts[id_] = DeliveryReceipt(**data)
            self.attendance = [json.loads(r) for (r,) in
                               cx.execute(sa.select(_ATT.c.data).order_by(_ATT.c.seq))]

    # ---------- write-through helpers used by the API ----------
    def save_space(self, code: str, space: dict, venue: Venue, channel: Channel) -> None:
        self.events_by_code[code] = space
        self.venues[venue.id] = venue
        self.channels[channel.id] = channel
        self._put_many([("space", code, space),
                        ("venue", venue.id, venue.model_dump()),
                        ("channel", channel.id, channel.model_dump())])

    def save_join(self, profile: Profile, endpoint: Endpoint,
                  attached_to: list, attendance_row: dict) -> None:
        self.profiles[profile.id] = profile
        self.endpoints[endpoint.id] = endpoint
        self.attachments[endpoint.id] = {"profile_id": profile.id,
                                         "attached_to": attached_to}
        self.attendance.append(attendance_row)
        ep = endpoint.model_dump(); ep["capabilities"] = sorted(endpoint.capabilities)
        self._put_many([("profile", profile.id, profile.model_dump()),
                        ("endpoint", endpoint.id, ep),
                        ("attach", endpoint.id, self.attachments[endpoint.id])])
        if self.engine:
            with self.engine.begin() as cx:
                cx.execute(sa.insert(_ATT).values(data=json.dumps(attendance_row, default=str)))

    def save_content_event(self, event: ContentEvent) -> None:
        self.events[event.id] = event
        self._put("cevent", event.id, event.model_dump())

    def save_delivery_batch(self, plans: list, receipts: list) -> None:
        rows = [("plan", p.id, p.model_dump()) for p in plans]
        rows += [("receipt", r.id, r.model_dump()) for r in receipts]
        self._put_many(rows)

    def revive_presence(self, endpoint_id: str):
        """A live WebSocket is proof of presence: recreate the session on reconnect."""
        att = self.attachments.get(endpoint_id)
        if not att:
            return None
        for s in self.presence.values():
            if s.endpoint_id == endpoint_id and not s.expired:
                return s
        s = PresenceSession(profile_id=att["profile_id"], endpoint_id=endpoint_id,
                            attached_to=att["attached_to"], ttl_seconds=14400)
        self.presence[s.id] = s
        return s

    def log(self, actor: str, action: str, resource: str) -> None:
        self.audit.append({"actor": actor, "action": action, "resource": resource})

    def erase_profile(self, profile_id: str) -> None:
        """GDPR art. 17 (W11): cascade profile + presence; pseudonymize receipts."""
        self.profiles.pop(profile_id, None)
        self.presence = {k: v for k, v in self.presence.items()
                         if v.profile_id != profile_id}
        gone_eps = [k for k, v in self.endpoints.items() if v.profile_id == profile_id]
        self.endpoints = {k: v for k, v in self.endpoints.items()
                          if v.profile_id != profile_id}
        for ep in gone_eps:
            self.attachments.pop(ep, None)
            self._delete_kv("endpoint", ep)
            self._delete_kv("attach", ep)
        self._delete_kv("profile", profile_id)
        changed = []
        for r in self.receipts.values():
            if r.profile_id == profile_id:
                r.profile_id = "prf_erased"
                changed.append(("receipt", r.id, r.model_dump()))
        for p in self.plans.values():
            if p.profile_id == profile_id:
                p.profile_id = "prf_erased"
                changed.append(("plan", p.id, p.model_dump()))
        self._put_many(changed)
        self.log("system", "erasure", profile_id)
