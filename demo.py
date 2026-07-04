"""Demo: one transit platform, three riders, one provider outage, one emergency.

Run:  python demo.py
"""
import asyncio
import json

from langlayer.models import (Channel, ContentEvent, ContextOverride, Endpoint,
                              LanguagePref, Modality, ModalityPref, PreferenceSet,
                              PresenceSession, PriorityClass, Profile, Venue)
from langlayer.providers import default_registry
from langlayer.render import metrics, process_event
from langlayer.store import Store


def rider(store, name, langs, mods, caps, attach):
    p = Profile(display_name=name, preferences=PreferenceSet(
        languages=[LanguagePref(tag=t, rank=i + 1) for i, t in enumerate(langs)],
        modalities=[ModalityPref(kind=m, rank=i + 1) for i, m in enumerate(mods)]))
    store.profiles[p.id] = p
    e = Endpoint(profile_id=p.id, kind="mobile", capabilities=caps)
    store.endpoints[e.id] = e
    s = PresenceSession(profile_id=p.id, endpoint_id=e.id, attached_to=attach)
    store.presence[s.id] = s
    return p


async def main():
    store, registry = Store(), default_registry()

    venue = Venue(name="Red Line — Central Platform")
    store.venues[venue.id] = venue
    chan = Channel(venue_id=venue.id, name="platform announcements")
    store.channels[chan.id] = chan
    attach = [f"venue:{venue.id}"]

    # Pre-translated announcement + emergency templates (W5/W6: cache-first)
    cache = registry.get("cache")
    cache.preload("arrival", {
        "es-MX": "El tren de la línea {line} llega en {min} minutos",
        "asl": "TRAIN {line} ARRIVE {min} MINUTES",
        "en": "The {line} line train arrives in {min} minutes"})
    cache.preload("evacuate", {
        "es-MX": "EMERGENCIA: evacúe la plataforma por la salida {exit}",
        "asl": "EMERGENCY EVACUATE PLATFORM EXIT {exit}",
        "en": "EMERGENCY: evacuate the platform via exit {exit}"})

    # Three riders, three different worlds, one platform
    rider(store, "Marisol", ["es-MX"], [Modality.speech, Modality.captions],
          {"audio_out", "text_out"}, attach)
    rider(store, "Devon", ["asl", "en"], [Modality.sign, Modality.captions],
          {"video_out", "text_out"}, attach)
    rider(store, "Ana", ["en"], [Modality.simplified],
          {"text_out"}, attach)

    def show(title, receipts):
        print(f"\n=== {title} ===")
        for r in receipts:
            who = store.profiles.get(r.profile_id)
            art = store.artifacts.get(r.artifact_id)
            print(f"  {who.display_name if who else r.profile_id:<8} "
                  f"-> {art.content if art else 'NOT DELIVERED'}")
            print(f"           source={r.source_used} failovers={r.failovers} "
                  f"e2e={r.e2e_ms}ms sla_met={r.sla_met} quality={r.quality}")
            if r.failover_causes:
                print(f"           causes={r.failover_causes}")

    # 1) Routine templated announcement -> cache serves everyone instantly
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.announcement,
                      kind="template_ref", template="arrival",
                      slots={"line": "Red", "min": 3})
    show("Templated arrival announcement (cache-first)", await process_event(ev, store, registry))

    # 2) Free-text announcement -> AI realtime renders per language/modality
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.announcement,
                      payload="Elevator at the north end is out of service today")
    show("Free-text announcement (cache miss -> AI realtime)", await process_event(ev, store, registry))

    # 3) Primary AI outage -> automatic failover (W7), receipts record it
    registry.get("ai-realtime").forced_outage = True
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.announcement,
                      payload="Shuttle buses replace trains after 10pm tonight")
    show("Same, during primary AI outage (failover chain)", await process_event(ev, store, registry))
    registry.get("ai-realtime").forced_outage = False

    # 4) Emergency -> deterministic template first, to every endpoint
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.emergency,
                      kind="template_ref", template="evacuate", slots={"exit": "B"})
    receipts = await process_event(ev, store, registry)
    show("EMERGENCY evacuation (template outranks live AI)", receipts)

    # A routing decision, fully explained (audit trail for regulators)
    plan = store.plans[receipts[0].plan_id]
    print("\n=== One plan's decision record (D1-D6) ===")
    print(json.dumps(plan.decisions, indent=2))

    print("\n=== Platform metrics (all derived from signed receipts) ===")
    print(json.dumps(metrics(store), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
