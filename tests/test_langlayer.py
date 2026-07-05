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
    assert p.ttfo_budget_ms == 300 and p.e2e_budget_ms == 8000


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
    devon = next(r for r in seed["riders"] if r["name"] == "Sandi")

    with client.websocket_connect(f"/v1/deliveries/subscribe/{devon['endpoint_id']}") as ws:
        resp = client.post(f"/v1/channels/{seed['channel_id']}/events",
                           json={"channel_id": seed["channel_id"],
                                 "priority_class": "announcement",
                                 "payload": "Elevator out of service"})
        assert resp.status_code == 200 and resp.json()["plans"] == 3
        msg = ws.receive_json()
        assert msg["type"] == "artifact" and msg["rider"] == "Sandi"
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
    assert {r["name"] for r in riders} >= {"Monica", "Sandi", "Nate"}
    receipts = client.get("/v1/receipts").json()
    assert receipts and receipts[0]["sla_met"] is True and receipts[0]["signature"]


def test_event_flow_end_to_end():
    """Access code -> join -> send -> receipt, attendance, summary, transcript."""
    from fastapi.testclient import TestClient
    from langlayer import api as api_mod
    client = TestClient(api_mod.app)

    bad = client.post("/v1/events", json={"name": "X", "invite_code": "wrong"})
    assert bad.status_code == 403

    ev = client.post("/v1/events", json={"name": "City Council",
                                         "invite_code": "letmein"}).json()
    code = ev["access_code"]
    assert code.startswith("LL-") and "/join?code=" in ev["attendee_link"]

    info = client.get(f"/v1/events/{code}").json()
    assert [l["name"] for l in info["languages"]][:3] == [
        "American Sign Language", "Amharic", "Arabic"]
    asl = info["languages"][0]
    assert asl["tier"] == "sign" and asl["voice"] is False

    j = client.post(f"/v1/events/{code}/join",
                    json={"kind": "business", "name": "Corner Cafe",
                          "language": "es", "modality": "captions"}).json()
    j2 = client.post(f"/v1/events/{code}/join",
                     json={"kind": "person", "name": "Devon",
                           "language": "asl", "modality": "captions"}).json()
    assert j2["format"] == "sign"  # ASL forces sign format

    with client.websocket_connect(f"/v1/deliveries/subscribe/{j['endpoint_id']}") as ws:
        r = client.post(f"/v1/channels/{info['channel_id']}/events",
                        json={"channel_id": info["channel_id"],
                              "priority_class": "announcement",
                              "payload": "The meeting starts in five minutes"})
        assert r.status_code == 200 and r.json()["plans"] == 2
        msg = ws.receive_json()
        assert msg["language"] == "es" and msg["delivered"]

    att = client.get(f"/v1/events/{code}/attendees").json()
    assert {a["name"] for a in att["log"]} == {"Corner Cafe", "Devon"}
    summ = client.get(f"/v1/events/{code}/summary").json()
    assert summ["delivered"] == 2 and summ["estimated_cost_usd"] > 0
    tr = client.get(f"/v1/events/{code}/transcript").json()
    assert tr["announcements"][0]["source_text"] == "The meeting starts in five minutes"
    assert len(tr["announcements"][0]["deliveries"]) == 2
    # zero retention: artifact content purged after delivery
    from langlayer import state as _st
    assert all(a.content == "[content not retained]"
               for a in _st.store.artifacts.values())


def test_new_pages_serve():
    from fastapi.testclient import TestClient
    from langlayer import api as api_mod
    client = TestClient(api_mod.app)
    for path in ("/host", "/join", "/console", "/security", "/demo", "/dashboard"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "\u2014" not in resp.text, f"em dash found in {path}"


def test_speak_endpoint_requires_live_mode(monkeypatch):
    import os
    from fastapi.testclient import TestClient
    from langlayer import api as api_mod
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(api_mod.app)
    seed = client.post("/v1/demo/seed").json()
    r = client.post(f"/v1/channels/{seed['channel_id']}/speak",
                    files={"audio": ("s.webm", b"x" * 500, "audio/webm")})
    assert r.status_code == 400 and "live mode" in r.json()["detail"]


def test_sign_label_is_honest():
    from fastapi.testclient import TestClient
    from langlayer import api as api_mod
    client = TestClient(api_mod.app)
    ev = client.post("/v1/events", json={"name": "T", "invite_code": "letmein"}).json()
    info = client.get(f"/v1/events/{ev['access_code']}").json()
    sign = next(f for f in info["formats"] if f["kind"] == "sign")
    assert "gloss" in sign["label"]


def test_coverage_and_homepage():
    from fastapi.testclient import TestClient
    from langlayer import api as api_mod
    client = TestClient(api_mod.app)
    cov = client.get("/v1/coverage").json()
    assert len(cov["languages"]) == 46
    names = [l["name"] for l in cov["languages"]]
    assert names == sorted(names)
    ar = next(l for l in cov["languages"] if l["tag"] == "ar")
    assert ar["rtl"] is True
    assert client.get("/").status_code == 200
    assert client.get("/coverage").status_code == 200
    assert "\u2014" not in client.get("/").text
    assert "\u2014" not in client.get("/coverage").text


def test_anthropic_provider_wiring(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from langlayer.providers import default_registry
    r = default_registry()
    assert type(r.get("ai-realtime")).__name__ == "AnthropicProvider"
    assert r.get("ai-realtime").model == "claude-fable-5"


def test_human_bridge_label_is_honest(world):
    import asyncio
    from langlayer.models import ContentEvent, PriorityClass
    from langlayer.render import process_event
    store, registry, venue, chan, *_ = world
    registry.get("ai-realtime").forced_outage = True
    registry.get("ai-realtime-alt").forced_outage = True
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="hello")
    r = asyncio.run(process_event(ev, store, registry))[0]
    assert r.source_used == "human-bridge"
    art = store.artifacts[r.artifact_id]
    assert "integration pending" in art.content


def test_durability_survives_restart(tmp_path):
    """Space, join, and receipts survive a full process restart (new Store)."""
    import asyncio
    from langlayer.models import ContentEvent, PriorityClass
    from langlayer.providers import default_registry
    from langlayer.render import process_event
    from langlayer.store import Store

    db = f"sqlite:///{tmp_path}/prod.db"
    from langlayer import api as api_mod
    from fastapi.testclient import TestClient

    from langlayer import state
    old_store = state.store
    try:
        state.store = Store(db_url=db)
        client = TestClient(api_mod.app)
        ev = client.post("/v1/events", json={"name": "Community Night",
                                             "invite_code": "letmein"}).json()
        code = ev["access_code"]
        j = client.post(f"/v1/events/{code}/join",
                        json={"kind": "person", "name": "Maria",
                              "language": "es", "modality": "captions"}).json()
        info = client.get(f"/v1/events/{code}").json()
        client.post(f"/v1/channels/{info['channel_id']}/events",
                    json={"channel_id": info["channel_id"],
                          "priority_class": "announcement", "payload": "hello"})

        # ---- simulate restart: brand new store from the same database ----
        state.store = Store(db_url=db)
        assert code in state.store.events_by_code
        assert any(a["name"] == "Maria" for a in state.store.attendance)
        assert len(state.store.receipts) >= 1
        # attendee reconnects: websocket revives presence, deliveries resume
        with client.websocket_connect(f"/v1/deliveries/subscribe/{j['endpoint_id']}") as ws:
            r = client.post(f"/v1/channels/{info['channel_id']}/events",
                            json={"channel_id": info["channel_id"],
                                  "priority_class": "announcement",
                                  "payload": "after the restart"})
            assert r.json()["plans"] == 1
            msg = ws.receive_json()
            assert msg["delivered"] and msg["language"] == "es"
        tr = client.get(f"/v1/events/{code}/transcript").json()
        assert len(tr["announcements"]) == 2  # both survive, pre and post restart
    finally:
        state.store = old_store


def test_fanout_renders_once_per_language(world):
    """50 attendees sharing a language cost one model call, 50 receipts."""
    import asyncio
    from langlayer.models import (ContentEvent, Endpoint, LanguagePref, Modality,
                                  ModalityPref, PreferenceSet, PresenceSession,
                                  PriorityClass, Profile)
    from langlayer.render import process_event
    store, registry, venue, chan, *_ = world
    for i in range(50):
        p = Profile(display_name=f"a{i}", preferences=PreferenceSet(
            languages=[LanguagePref(tag="es", rank=1)],
            modalities=[ModalityPref(kind=Modality.captions, rank=1)]))
        store.profiles[p.id] = p
        e = Endpoint(profile_id=p.id, capabilities={"text_out"})
        store.endpoints[e.id] = e
        s = PresenceSession(profile_id=p.id, endpoint_id=e.id,
                            attached_to=[f"venue:{venue.id}"])
        store.presence[s.id] = s

    prov = registry.get("ai-realtime")
    calls = {"n": 0}
    orig = prov.render
    async def counting(plan, event):
        calls["n"] += 1
        return await orig(plan, event)
    prov.render = counting

    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.announcement,
                      payload="One message, many people")
    receipts = asyncio.run(process_event(ev, store, registry))
    es_receipts = [r for r in receipts if store.plans[r.plan_id].language == "es"]
    assert len(es_receipts) == 50
    assert all(r.delivered and r.signature for r in es_receipts)
    assert calls["n"] <= 3  # one per (language x modality) group, not per person


def test_rate_limit_blocks_flood():
    from fastapi.testclient import TestClient
    from langlayer import api as api_mod
    from langlayer import ratelimit
    ratelimit._buckets.clear()
    client = TestClient(api_mod.app)
    codes = [client.post("/v1/events", json={"name": f"s{i}",
                                             "invite_code": "letmein"}).status_code
             for i in range(8)]
    assert codes[:5] == [200] * 5 and 429 in codes[5:]
    ratelimit._buckets.clear()


def test_quality_judge_parser():
    from langlayer.quality import _parse
    assert _parse("0.92") == 0.92
    assert _parse("Score: 0.85") == 0.85
    assert _parse("1.0") == 1.0
    assert _parse("no number here") is None


def test_interpreter_contract_shapes():
    from langlayer.interpreter_bridge import (DispatchClient, InterpreterAssignment,
                                              InterpreterRequest, SessionReport)
    req = InterpreterRequest(request_id="r1", space_name="Clinic", source_language="en",
                             target_language="asl", modality="sign",
                             priority_class="conversational", compliance_mode="hipaa",
                             context_summary="clinic lobby", max_wait_seconds=60)
    assert req.compliance_mode == "hipaa"


def test_demo_templates_survive_registry_recreation():
    """Redeploy simulation: a fresh registry still serves demo templates."""
    import asyncio
    from langlayer.models import ContentEvent, PriorityClass
    from langlayer.providers import default_registry
    from langlayer.render import process_event
    from langlayer.store import Store
    from langlayer.models import (Channel, Endpoint, LanguagePref, Modality,
                                  ModalityPref, PreferenceSet, PresenceSession,
                                  Profile, Venue)
    store, registry = Store(), default_registry()  # brand new, no seed call
    venue = Venue(name="V"); store.venues[venue.id] = venue
    chan = Channel(venue_id=venue.id, name="c"); store.channels[chan.id] = chan
    p = Profile(display_name="X", preferences=PreferenceSet(
        languages=[LanguagePref(tag="es", rank=1)],
        modalities=[ModalityPref(kind=Modality.captions, rank=1)]))
    store.profiles[p.id] = p
    e = Endpoint(profile_id=p.id, capabilities={"text_out"}); store.endpoints[e.id] = e
    s = PresenceSession(profile_id=p.id, endpoint_id=e.id,
                        attached_to=[f"venue:{venue.id}"]); store.presence[s.id] = s
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.announcement,
                      kind="template_ref", template="arrival",
                      slots={"line": "Red", "min": 3})
    rec = asyncio.run(process_event(ev, store, registry))[0]
    assert rec.delivered and rec.source_used == "cache" and rec.failovers == 0


def test_last_tier_delivers_late_instead_of_failing(world):
    """Budget exhaustion must not produce silence: the final tier gets a
    grace window; delivery succeeds and the receipt honestly marks SLA."""
    import asyncio
    from langlayer.models import ContentEvent, PriorityClass
    from langlayer.render import process_event
    store, registry, venue, chan, *_ = world
    # Force both AI tiers to consume the whole budget via outage + slow sim
    registry.get("ai-realtime").forced_outage = True
    registry.get("ai-realtime-alt").base_latency_ms = 99999  # will time out
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.conversational,
                      payload="hello")
    r = asyncio.run(process_event(ev, store, registry))[0]
    assert r.delivered, r.failover_causes
    assert r.source_used == "human-bridge"
    assert r.failovers == 2


def test_sign_chain_exhaustion_falls_back_to_captions(world):
    import asyncio
    from langlayer.models import (Channel, ContentEvent, Endpoint, LanguagePref,
                                  Modality, ModalityPref, PreferenceSet,
                                  PresenceSession, PriorityClass, Profile)
    from langlayer.render import process_event
    store, registry, venue, chan, *_ = world
    p = Profile(display_name="SignUser", preferences=PreferenceSet(
        languages=[LanguagePref(tag="asl", rank=1)],
        modalities=[ModalityPref(kind=Modality.sign, rank=1)]))
    store.profiles[p.id] = p
    e = Endpoint(profile_id=p.id, capabilities={"video_out", "text_out"})
    store.endpoints[e.id] = e
    s = PresenceSession(profile_id=p.id, endpoint_id=e.id,
                        attached_to=[f"venue:{venue.id}"])
    store.presence[s.id] = s
    registry.get("ai-realtime").forced_outage = True
    registry.get("ai-realtime-alt").forced_outage = True
    hb = registry.get("human-bridge")
    for _ in range(3):
        hb.circuit.record_failure()  # open the circuit: D5 routes around it
    ev = ContentEvent(channel_id=chan.id, priority_class=PriorityClass.live,
                      payload="Doors open at seven")
    recs = asyncio.run(process_event(ev, store, registry))
    rec = next(r for r in recs if r.profile_id == p.id)
    assert rec.delivered and rec.source_used == "captions-fallback"
    assert "sign video: no capable source" in " ".join(rec.failover_causes)
