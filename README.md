# Language Layer — reference implementation

Working backend for the Language Layer spec (`language-layer-spec.md`): the routing
engine (decisions D1–D6), provider source chains with automatic failover and circuit
breakers, delivery receipts signed and SLA-evaluated (D7), presence with TTL expiry,
live language/modality switching, GDPR erasure, and metrics derived entirely from
receipts.

## Run it

```bash
pip install fastapi uvicorn pytest httpx

python demo.py                      # CLI end-to-end transit scenario
python prove.py                     # generates PROOF-REPORT.md (evidence run)
python -m pytest tests/ -q          # 12 tests (incl. HTTP+WebSocket end-to-end)
uvicorn langlayer.api:app --reload  # then open http://localhost:8000/demo
```

## The live demo (the thing you show OpenAI)

Three URLs once the server is up:

- `/demo` — operator console + three live phones (the pitch itself)
- `/rider` — standalone mobile endpoint: open it on an actual phone, pick a
  profile, and announcements arrive in that person's language (emergency
  deliveries vibrate). Deep-linkable: `/rider?endpoint_id=...`
- `/dashboard` — live operations: receipt-derived metrics, provider health
  with circuit states, and a rolling table of signed receipts

`/demo` is an operator console and three live "phones"
(Marisol: es-MX speech · Devon: ASL · Ana: simplified English). Type any
announcement and watch it arrive on all three simultaneously, each in that
person's language and modality, with source/latency/SLA badges per delivery.
Buttons fire the templated arrival, the emergency evacuation (deterministic
template outranks live AI), and a primary-AI outage toggle so the audience can
watch failover happen in the receipts. Tap any bubble to see that delivery's
full D1–D6 decision record.

**Real models:** `export OPENAI_API_KEY=sk-...` before starting and the registry
swaps the simulated AI providers for live OpenAI calls (`providers_openai.py`) —
same adapter contract, zero engine changes. Without a key everything runs
simulated, so the demo works offline too (recommended backup for the meeting).

The demo puts three riders with different profiles (Spanish speech, ASL sign video,
simplified-English captions) on one transit platform, then runs: a templated
announcement (cache-first, ~0 ms), a free-text announcement (AI realtime), the same
during a forced primary-AI outage (automatic failover, recorded in receipts), and an
emergency evacuation (deterministic template outranks live AI by policy). It ends by
printing one plan's full D1–D6 decision record and the platform metrics.

## Layout

```
langlayer/models.py     data models, latency budgets, default source chains
langlayer/routing.py    the decision engine (pure, auditable)
langlayer/providers.py  provider adapter contract, simulated providers, circuit breaker
langlayer/render.py     failover execution, signed receipts, SLA, metrics
langlayer/store.py            storage (in-memory; Postgres-interface-compatible)
langlayer/api.py              FastAPI surface + WS delivery + /demo page
langlayer/providers_openai.py real OpenAI adapter (auto-enabled via OPENAI_API_KEY)
langlayer/delivery.py         WebSocket delivery hub
langlayer/static/demo.html    the live pitch demo UI
demo.py, prove.py, tests/, deploy/ (Dockerfile, compose, Fly, Render), PILOT-KIT.md
```

## What's simulated vs. real

Real: every decision, the failover machinery, circuit breakers, budgets, receipts,
signatures, SLA evaluation, metrics, erasure. Simulated: the providers — each is a
stub behind the exact adapter contract (`render(plan, event) -> Artifact`) the
production OpenAI Realtime adapter implements, so swapping in real models touches
`providers.py` only, never the engine. Streaming ingest/delivery (WebSocket/WebRTC)
and durable storage are Phase 1 per the spec's build plan.
