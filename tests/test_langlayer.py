import asyncio

import pytest

from langlayer.models import (Channel, ContentEvent, ContextOverride, Endpoint,
                              LanguagePref, Modality, ModalityPref, PreferenceSet,
                              PresenceSession, PriorityClass, Profile, Venue)
from langlayer.providers import default_registry
from langlayer.render import metrics, process_event
from langlayer.routing import route
from langlayer.store import Store


@pytest.fixture()
def world():
    store, registry = Store(), default_registry()
    venue = Venue(name="Clinic Room 4", compliance_mode="hipaa")
    store.venues[venue.id] = venue
    chan = Channel(venue_id=venue.id, name="consult")
    store.channels[chan.id] = chan

    profile = Profile(display_name="Devon", preferences=PreferenceSet(
        languages=[LanguagePref(tag="asl", rank=1), LanguagePref(tag="en", rank=2)],
        modalities=[ModalityPref(kind=Modality.sign, rank=1),
                    ModalityPref(kind=Modality.captions, rank=2)]))
    store.profiles[profile.id] = profile
    endpoint = Endpoint(profile_id=profile.id,
                        capabilities={"video_out", "text_out"})
    store.endpoints[endpoint.id] = endpoint
    session = PresenceSession(profile_id=profile.id, endpoint_id=endpoint.id,
                              attached_to=[f"venue:{venue.id}"])
    store.presence[session.id] = session
    return store, registry, venue, chan, profile, endpoint


def health(registry):
    return {n: p.circuit.state for n, p in registry.providers.items()}


def test_routing_decisions_recorded(world):
    store, registry, venue, chan, profile, endpoint = world
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="Your test results look good")
    plans = route(ev, store, health(registry))
    assert len(plans) == 1
    p = plans[0]
    assert p.language == "asl" and p.modality == Modality.sign
    assert p.endpoint_id == endpoint.id
    assert set(p.decisions) == {"D1", "D2", "D3", "D4", "D5", "D6"}
    assert p.ttfo_budget_ms == 300 and p.e2e_budget_ms == 1000


def test_modality_respects_endpoint_capabilities(world):
    store, registry, venue, chan, profile, endpoint = world
    endpoint.capabilities = {"text_out"}  # no video -> sign impossible
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="hello")
    plan = route(ev, store, health(registry))[0]
    assert plan.modality == Modality.captions  # rank-2 compatible preference


def test_expired_presence_excluded(world):
    store, registry, venue, chan, profile, endpoint = world
    for s in store.presence.values():
        s.last_heartbeat_ms -= 10 * 60 * 1000
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.announcement,
                      payload="x")
    assert route(ev, store, health(registry)) == []


def test_context_override(world):
    store, registry, venue, chan, profile, endpoint = world
    profile.preferences.overrides.append(ContextOverride(
        context=f"venue:{venue.id}", languages=["en"],
        modalities=[Modality.captions]))
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="hello")
    plan = route(ev, store, health(registry))[0]
    assert plan.language == "en" and plan.modality == Modality.captions


def test_live_switch(world):
    store, registry, venue, chan, profile, endpoint = world
    profile.session_override = {"language": "en"}
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="hello")
    plan = route(ev, store, health(registry))[0]
    assert plan.language == "en" and "switch" in plan.decisions["D2"]


def test_emergency_chain_is_cache_first(world):
    store, registry, venue, chan, *_ = world
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.emergency,
                      kind="template_ref", template="evac", slots={})
    plan = route(ev, store, health(registry))[0]
    assert plan.source_chain[0].provider == "cache"


def test_failover_on_provider_outage(world):
    store, registry, venue, chan, *_ = world
    registry.get("ai-realtime").forced_outage = True
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="hello")
    receipts = asyncio.run(process_event(ev, store, registry))
    r = receipts[0]
    assert r.delivered and r.failovers >= 1
    assert r.source_used == "ai-realtime-alt"
    assert any("unavailable" in c for c in r.failover_causes)


def test_circuit_opens_and_chain_skips_it(world):
    store, registry, venue, chan, *_ = world
    prov = registry.get("ai-realtime")
    prov.forced_outage = True
    for _ in range(3):
        ev = ContentEvent(channel_id=chan.id,
                          priority_class=PriorityClass.conversational, payload="x")
        asyncio.run(process_event(ev, store, registry))
    assert prov.circuit.state == "open"
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="x")
    plan = route(ev, store, health(registry))[0]
    assert all(s.provider != "ai-realtime" for s in plan.source_chain)


def test_receipt_signed_and_sla_evaluated(world):
    store, registry, venue, chan, *_ = world
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="hello")
    r = asyncio.run(process_event(ev, store, registry))[0]
    assert r.signature and len(r.signature) == 64
    assert r.sla_met and r.e2e_ms <= 1000
    m = metrics(store)
    assert m["delivery_success_rate"] == 1.0 and m["e2e_ms"]["p95"] is not None


def test_erasure_pseudonymizes_receipts(world):
    store, registry, venue, chan, profile, endpoint = world
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.announcement,
                      payload="hello")
    asyncio.run(process_event(ev, store, registry))
    store.erase_profile(profile.id)
    assert profile.id not in store.profiles
    assert all(r.profile_id == "prf_erased" for r in store.receipts.values())
    assert not any(s.profile_id == profile.id for s in store.presence.values())


def test_cache_miss_does_not_open_circuit(world):
    store, registry, venue, chan, *_ = world
    for _ in range(5):  # free-text announcements all miss the cache
        ev = ContentEvent(channel_id=chan.id,
                          priority_class=PriorityClass.announcement, payload="x")
        asyncio.run(process_event(ev, store, registry))
    assert registry.get("cache").circuit.state == "closed"


def test_demo_seed_ws_subscribe_and_live_push():
    """End-to-end over HTTP+WS: seed -> subscribe -> ingest -> artifact pushed."""
    from fastapi.testclient import TestClient
    from langlayer import api as api_mod
    client = TestClient(api_mod.app)

    seed = client.post("/v1/demo/seed").json()
    assert len(seed["riders"]) == 3 and seed["provider_mode"] in ("simulated", "openai")
    devon = next(r for r in seed["riders"] if r["name"] == "Devon")

    with client.websocket_connect(f"/v1/deliveries/subscribe/{devon['endpoint_id']}") as ws:
        resp = client.post(f"/v1/channels/{seed['channel_id']}/events",
                           json={"channel_id": seed["channel_id"],
                                 "priority_class": "announcement",
                                 "payload": "Elevator out of service"})
        assert resp.status_code == 200 and resp.json()["plans"] == 3
        msg = ws.receive_json()
        assert msg["type"] == "artifact" and msg["rider"] == "Devon"
        assert msg["language"] == "asl" and msg["modality"] == "sign"
        assert msg["delivered"] and set(msg["decisions"]) == {"D1","D2","D3","D4","D5","D6"}

    page = client.get("/demo")
    assert page.status_code == 200 and "LANGUAGE" in page.text


def test_rider_dashboard_and_data_endpoints():
    from fastapi.testclient import TestClient
    from langlayer import api as api_mod
    client = TestClient(api_mod.app)
    seed = client.post("/v1/demo/seed").json()
    client.post(f"/v1/channels/{seed['channel_id']}/events",
                json={"channel_id": seed["channel_id"],
                      "priority_class": "announcement", "payload": "test"})
    assert client.get("/rider").status_code == 200
    assert client.get("/dashboard").status_code == 200
    riders = client.get("/v1/demo/riders").json()
    assert {r["name"] for r in riders} >= {"Marisol", "Devon", "Ana"}
    receipts = client.get("/v1/receipts").json()
    assert receipts and receipts[0]["sla_met"] is True and receipts[0]["signature"]
