"""Tests for the stateless POST /v1/render endpoint (core.py)."""
import pytest
from fastapi.testclient import TestClient

from langlayer import api as api_mod
from langlayer import state

PAYLOAD = "Shelter open at the community center. Water and charging available."


@pytest.fixture()
def client():
    return TestClient(api_mod.app)


def test_render_happy_path(client):
    """One payload fans out to N variants; 'text' aliases to 'translation'; dupes dropped."""
    r = client.post("/v1/render", json={
        "payload": PAYLOAD,
        "source_language": "en",
        "targets": [
            {"language": "es", "modality": "text"},   # alias -> translation
            {"language": "zh"},                        # default modality
            {"language": "en", "modality": "simplified"},
            {"language": "es", "modality": "text"},    # duplicate -> deduped
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["event_id"].startswith("evt_")
    variants = body["variants"]
    assert len(variants) == 3, f"expected 3 after dedupe, got {len(variants)}"
    pairs = {(v["language"], v["modality"]) for v in variants}
    assert ("es", "translation") in pairs          # "text" normalized to canonical name
    assert ("en", "simplified") in pairs
    for v in variants:
        assert v["content"]
        assert v["untranslated"] is False          # a provider translated it


def test_render_tier4_floor(client):
    """Every AI provider down -> still return the original text, clearly labelled."""
    ai = [state.registry.get(n) for n in ("ai-realtime", "ai-realtime-alt", "ai-batch")]
    ai = [p for p in ai if p is not None]
    if not ai or not all(hasattr(p, "forced_outage") for p in ai):
        pytest.skip("floor test requires simulated providers (no live API keys)")
    for p in ai:
        p.forced_outage = True
    try:
        r = client.post("/v1/render", json={
            "payload": "Road washed out, take Main St.",
            "targets": [{"language": "es"}],
        })
        assert r.status_code == 200, r.text
        v = r.json()["variants"][0]
        assert v["untranslated"] is True
        assert v["source_used"] == "pa-passthrough"
        assert "untranslated notice" in v["content"]
    finally:
        for p in ai:
            p.forced_outage = False


def test_render_caps_and_validation(client):
    base = {"payload": "hi"}
    assert client.post("/v1/render", json={**base, "targets": [{"language": "es"}] * 26}
                       ).status_code == 422                      # too many targets
    assert client.post("/v1/render", json={**base, "targets": []}
                       ).status_code == 422                      # no targets
    assert client.post("/v1/render", json={"payload": "", "targets": [{"language": "es"}]}
                       ).status_code == 422                      # empty payload
    assert client.post("/v1/render", json={**base, "targets": [{"language": "zz"}]}
                       ).status_code == 422                      # unknown language
    assert client.post("/v1/render",
                       json={**base, "targets": [{"language": "es", "modality": "bogus"}]}
                       ).status_code == 422                      # unknown modality


def test_render_no_metrics_pollution(client):
    """Stateless renders must not enter the shared delivery receipts/metrics."""
    before_r = len(state.store.receipts)
    before_a = len(state.store.artifacts)
    r = client.post("/v1/render", json={
        "payload": PAYLOAD, "targets": [{"language": "es"}, {"language": "fr"}]})
    assert r.status_code == 200, r.text
    assert len(state.store.receipts) == before_r    # rendered into a throwaway Store
    assert len(state.store.artifacts) == before_a


def test_render_rate_limited(client):
    """The endpoint enforces the shared limiter. Drain this IP's render bucket first so
    the assertion is deterministic (looping ~cap times races token refill)."""
    import time
    from langlayer import ratelimit
    ip = "203.0.113.7"                                   # dedicated bucket for this test
    ratelimit._buckets[(ip, "render")] = [0.0, time.monotonic()]   # empty it
    r = client.post("/v1/render", headers={"x-forwarded-for": ip},
                    json={"payload": "x", "targets": [{"language": "es"}]})
    assert r.status_code == 429, r.text
