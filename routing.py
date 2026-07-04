"""Routing Engine — spec §1.2. Pure decision logic, D1–D6.

Input:  (event, store state, provider health)
Output: one DeliveryPlan per recipient, with every decision's reason recorded.
Execution lives elsewhere (render.py); this module never touches a provider.
"""
from __future__ import annotations

from .models import (DEFAULT_CHAINS, LATENCY_BUDGETS, MODALITY_NEEDS, ChainStep,
                     ContentEvent, DeliveryPlan, Endpoint, Modality, PreferenceSet,
                     PresenceSession, Profile)
from .store import Store


def _override_for(prefs: PreferenceSet, contexts: list[str]) -> tuple[list[str], list[Modality]]:
    langs: list[str] = []
    mods: list[Modality] = []
    for ov in prefs.overrides:
        if ov.context in contexts:
            langs = ov.languages or langs
            mods = ov.modalities or mods
    return langs, mods


def _pick_language(profile: Profile, contexts: list[str]) -> tuple[str, str]:
    if profile.session_override and profile.session_override.get("language"):
        return profile.session_override["language"], "D2: live session switch (W9)"
    ov_langs, _ = _override_for(profile.preferences, contexts)
    if ov_langs:
        return ov_langs[0], f"D2: context override for {contexts}"
    ranked = sorted(profile.preferences.languages, key=lambda l: l.rank)
    if ranked:
        return ranked[0].tag, "D2: top-ranked profile language"
    return "en", "D2: no preference on file; default en"


def _pick_modality(profile: Profile, endpoint: Endpoint,
                   contexts: list[str]) -> tuple[Modality, str]:
    if profile.session_override and profile.session_override.get("modality"):
        return Modality(profile.session_override["modality"]), "D3: live session switch (W9)"
    _, ov_mods = _override_for(profile.preferences, contexts)
    candidates = ov_mods or [m.kind for m in
                             sorted(profile.preferences.modalities, key=lambda m: m.rank)]
    for kind in candidates:
        if MODALITY_NEEDS[kind] <= endpoint.capabilities:
            why = "context override" if ov_mods else "top-ranked compatible with endpoint"
            return kind, f"D3: {why} ({endpoint.kind}:{sorted(endpoint.capabilities)})"
    return Modality.captions, "D3: fallback captions (no compatible preference)"


def _pick_endpoint(sessions: list[PresenceSession], store: Store) -> tuple[Endpoint, str]:
    active = [s for s in sessions if s.attention == "active"]
    chosen = (active or sessions)[0]
    why = "active attention" if active else "most recent presence (passive)"
    return store.endpoints[chosen.endpoint_id], f"D4: {why} on {chosen.endpoint_id}"


def _source_chain(event: ContentEvent, registry_health: dict[str, str]) -> tuple[list[ChainStep], str]:
    names = DEFAULT_CHAINS[event.priority_class]
    steps, skipped = [], []
    for i, n in enumerate(names):
        if registry_health.get(n) == "open":
            skipped.append(n)
            continue
        steps.append(ChainStep(provider=n, role="primary" if not steps else "fallback"))
    if not steps:  # every circuit open: try them anyway rather than deliver nothing
        steps = [ChainStep(provider=n, role="fallback") for n in names]
    why = f"D5: class default for {event.priority_class.value}"
    if skipped:
        why += f"; skipped open circuits {skipped}"
    if event.priority_class.value == "emergency":
        why += " (deterministic template outranks live AI by policy)"
    return steps, why


def route(event: ContentEvent, store: Store,
          registry_health: dict[str, str]) -> list[DeliveryPlan]:
    channel = store.channels[event.channel_id]
    contexts = [f"channel:{channel.id}", f"venue:{channel.venue_id}",
                f"class:{event.priority_class.value}"]

    # D1 — audience: everyone with a live presence attached to this channel/venue
    audience: dict[str, list[PresenceSession]] = {}
    for s in store.presence.values():
        if s.expired:
            continue
        if f"channel:{channel.id}" in s.attached_to or f"venue:{channel.venue_id}" in s.attached_to:
            audience.setdefault(s.profile_id, []).append(s)

    ttfo, e2e = LATENCY_BUDGETS[event.priority_class]
    plans: list[DeliveryPlan] = []
    for profile_id, sessions in audience.items():
        profile = store.profiles[profile_id]
        language, d2 = _pick_language(profile, contexts)
        endpoint, d4 = _pick_endpoint(sessions, store)
        modality, d3 = _pick_modality(profile, endpoint, contexts)
        chain, d5 = _source_chain(event, registry_health)
        plans.append(DeliveryPlan(
            event_id=event.id, profile_id=profile_id, language=language,
            modality=modality, endpoint_id=endpoint.id, source_chain=chain,
            ttfo_budget_ms=ttfo, e2e_budget_ms=e2e, priority_class=event.priority_class,
            decisions={
                "D1": f"attached to {contexts[0]} or {contexts[1]} with live presence",
                "D2": d2, "D3": d3, "D4": d4, "D5": d5,
                "D6": f"budget ttfo={ttfo}ms e2e={e2e}ms for class {event.priority_class.value}",
            }))
    return plans
