"""In-memory store. Interface-compatible with a Postgres-backed implementation;
presence is intentionally volatile (spec §1.6: presence is ephemeral by design)."""
from __future__ import annotations

from .models import (Artifact, Channel, ContentEvent, DeliveryPlan,
                     DeliveryReceipt, Endpoint, PresenceSession, Profile, Venue)


class Store:
    def __init__(self) -> None:
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

    def log(self, actor: str, action: str, resource: str) -> None:
        self.audit.append({"actor": actor, "action": action, "resource": resource})

    def erase_profile(self, profile_id: str) -> None:
        """GDPR art. 17 (W11): cascade profile + presence; pseudonymize receipts."""
        self.profiles.pop(profile_id, None)
        self.presence = {k: v for k, v in self.presence.items()
                         if v.profile_id != profile_id}
        self.endpoints = {k: v for k, v in self.endpoints.items()
                          if v.profile_id != profile_id}
        for r in self.receipts.values():
            if r.profile_id == profile_id:
                r.profile_id = "prf_erased"
        for p in self.plans.values():
            if p.profile_id == profile_id:
                p.profile_id = "prf_erased"
        self.log("system", "erasure", profile_id)
