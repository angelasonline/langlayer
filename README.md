# Language Layer

Language access, turned on like Wi-Fi. A space (a school, clinic, transit
line, or event) posts one QR code; everyone in it receives every announcement
in their own language and format: text, plain language, or spoken aloud.
No app, no account. Ask once, understood always.

**Live:** https://langlayer.onrender.com

## What it does

- 46 languages including American Sign Language (text gloss today) and
  Plain Language, with honest per-language capability flags (`/coverage`)
- Spaces with access codes: host console, attendee QR join, live attendee log
- Voice in (push-to-talk transcription) and spoken audio out (device voices,
  with captions fallback where no voice exists)
- Multi-provider AI translation: Anthropic Claude primary and OpenAI failover
  when both keys are set; automatic failover chains with circuit breakers
- A signed delivery receipt for every message: latency, source, SLA result,
  and a measured quality score (LLM-as-judge, scored after delivery)
- Durable storage (Postgres via DATABASE_URL, SQLite fallback): spaces,
  attendees, transcripts, and receipts survive restarts; attendees reconnect
  automatically
- Emergency priority class served from deterministic templates, never
  waiting on a model
- Per-IP rate limiting, exportable event transcripts, per-space cost meter
- Human interpreter dispatch: architecture complete
  (`langlayer/interpreter_bridge.py`); live interpreter network integration
  pending

## Run it locally

    pip install -r requirements.txt pytest
    python -m pytest tests/ -q          # 25 tests
    uvicorn langlayer.api:app --reload  # then open http://localhost:8000

Pages: `/` home, `/host` create a space, `/join?code=...` attendee,
`/console?code=...` host console, `/demo` three-persona walkthrough,
`/dashboard` operations, `/coverage` language capability map, `/healthz`.

Environment: `ANTHROPIC_API_KEY` and/or `OPENAI_API_KEY` (live translation
and quality judging; simulated without), `DATABASE_URL` (Postgres),
`HOST_INVITE_CODE` (gates space creation), `SENTRY_DSN` (optional).

## Verify the claims

    python demo.py       # CLI end-to-end scenario
    python prove.py      # writes PROOF-REPORT.md
    python loadtest.py --users 500   # against a running server

Load result on 1 CPU: 500 concurrent attendees, 100% of deliveries,
sub-second median fan-out. Operational envelope and production setup:
`deploy/PRODUCTION.md`. Accessibility posture: `ACCESSIBILITY.md`.
Pilot playbook: `PILOT-KIT.md`.

## Layout

    langlayer/routers/    API split: spaces, core delivery, pages
    langlayer/routing.py  the decision engine (pure, auditable, D1-D6)
    langlayer/render.py   render-once fan-out, receipts, SLA, metrics
    langlayer/providers*.py  provider contract, Anthropic + OpenAI adapters
    langlayer/store.py    write-through durable storage + migrations
    langlayer/quality.py  post-delivery quality judging
    langlayer/ratelimit.py, delivery.py, state.py, interpreter_bridge.py
    langlayer/static/     the product UI
    tests/, deploy/, loadtest.py, prove.py
