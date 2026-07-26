# Translate-then-Forward: Buzz × Langlayer × Bitchat

Prototype that closes the loop between three civic-infra pieces:

- **Buzz** — where an organizer posts one announcement.
- **Langlayer** — renders that announcement into N languages/formats.
- **Bitchat** — delivers each variant to a *place* (geohash), offline-capable.

Langlayer today can translate offline (its Tier-3 mesh LLM) but has **no way to
*carry* a message across a dead internet to phones in an area**. Bitchat is exactly
that missing delivery layer. This bridge is the glue.

```
Buzz announcements channel (kind-9)
        │  buzz messages get
        ▼
  LanglayerClient.render(text, [en, es, zh, en:simplified, ...])
        │  → one Variant per language/modality
        ▼
  one signed kind-20000 per variant  ["g", geohash]["l", lang]["m", modality]["n", label]
        │
        ▼
  published to the relays geo-nearest the geohash (the phone's own subscription set)
```

## Run it

Dry-run (default — nothing is published), offline stub translator, reads the live
Buzz announcements channel:

```bash
python3 translate_forward.py --preview --no-remote \
  --languages en es zh en:simplified ht
```

Against a real Langlayer server, add `--langlayer-url http://localhost:8000`
(or set `LANGLAYER_URL`). Go live with `--live` (publishes to Bitchat relays).

Design guarantees carried over from `bridge.py`:
- **Fail-open (Langlayer Tier-4).** If Langlayer is unreachable, the original text is
  still emitted — a message is never dropped, only downgraded.
- **Right relays.** Reuses `select_relays` — publishes to the N relays nearest the
  geohash center, the exact set the phone subscribes to (the fix that made `1r23b` work).
- **Attributable, deduped.** Persistent labeled key; distinct event id per variant.

## What needs to change in the Langlayer repo (github.com/angelasonline/langlayer)

Langlayer's current rendering path is **attendee-driven**: `POST /v1/channels/{cid}/events`
renders a variant *per joined attendee* and delivers to each attendee's own endpoint,
read back via `GET /v1/events/{code}/transcript`. That is stateful and assumes an
attendee has joined for every language you want. A transport bridge needs to ask for
languages directly. Two additions, smallest first:

### 1. (Required) Stateless render endpoint — `POST /v1/render`

A sibling of the existing events endpoint that returns the rendered artifacts directly
instead of delivering them to attendee endpoints. It reuses the machinery already in
`langlayer/routing.py` (`route`) and `langlayer/providers.py` (`Provider.render` →
`Artifact`) — no new model code, just a new entrypoint that doesn't require attendees.

Request:
```json
{
  "payload": "Shelter open at the community center. Water and charging available.",
  "source_language": "en",
  "priority_class": "announcement",
  "targets": [
    {"language": "es", "modality": "text"},
    {"language": "zh", "modality": "text"},
    {"language": "en", "modality": "simplified"}
  ]
}
```

Response (each item is essentially an `Artifact`):
```json
{
  "event_id": "evt_...",
  "variants": [
    {"language": "es", "modality": "text", "content": "Refugio abierto...",
     "provider": "anthropic", "quality_estimate": 0.98, "source_used": "cloud"},
    {"language": "zh", "modality": "text", "content": "社区中心...",
     "provider": "anthropic", "quality_estimate": 0.97, "source_used": "cloud"}
  ]
}
```

`translate_forward.py`'s `LanglayerClient._render_http` is already written against this
exact shape — ship the endpoint and the bridge talks to real providers with no change.

Suggested home: `langlayer/routers/core.py`, next to `POST /v1/channels/{cid}/events`.

### 2. (Recommended) Pluggable delivery sink — a Bitchat/Nostr provider

Longer-term, the cleaner architecture is to make **delivery** pluggable the same way
**providers** already are, so Langlayer can push variants outward without a poller in
the middle. Add a delivery adapter interface (mirroring `interpreter_bridge.py`'s
vendor-agnostic `DispatchClient` pattern) and a `NostrGeohashSink` implementation that
publishes each `Artifact` as a kind-20000 to the geohash's nearest relays. The
geohash-routing + signing code in this repo's `bridge.py` (`select_relays`,
`build_event`, `publish`) is a drop-in reference implementation to port over.

This turns "Buzz → poll → translate → forward" into "announce → Langlayer fans out to
every configured sink (web attendees **and** Bitchat mesh) in one pass."

### 3. (Ecosystem note — Bitchat client, not the Langlayer repo)

The bridge tags every variant with `["l", <lang>]` and `["m", <modality>]`. Bitchat
ignores unknown tags today, so all variants show in the channel (fine for a demo, noisy
at scale). The clean end-state is a small Bitchat-client change: **filter geohash
messages by the `l` tag to the user's chosen language.** Then one organizer post →
each phone shows exactly its own language, over mesh. Worth raising with the Bitchat
project; not blocking for the pilot.

## Status

- `translate_forward.py` — prototype, tested end-to-end in dry-run against the live
  Buzz announcements channel. Offline stub proves the fan-out + signing + geo-relay
  path with no server. Fail-open verified.
- Blocked on **item 1** to render real translations instead of the stub.
