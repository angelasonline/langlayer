# buzz-ops-bridge

Civic-messaging bridge: get an organizer's announcement to a **place**, in every
**language**, over a **resilient** path that survives when normal channels are down.

It does two things, from one Buzz **announcements** channel:

1. **Mirror** — copy each announcement to a Nostr **geohash** channel (Bitchat's
   location-channel format) so phones physically near a place can receive it
   (`bridge.py`).
2. **Translate-then-forward** — fan one announcement into **N language variants**
   via Langlayer, and forward each to the geohash so every phone sees the message
   in its own language (`translate_forward.py`).

This is a **self-mirror**, not a broadcast to strangers:

- Destination geohash defaults to `1r23b` — **Point Nemo**, the oceanic pole of
  inaccessibility (the farthest point on Earth from any land; no residents). The owner
  teleports their own Bitchat client to that cell to receive. (Avoid `s0000`: its center
  is beside null-island (0,0), where GPS-error clients congregate — not private.)
- Publishing identity is **labeled and persistent** (`n=buzz-ops-bridge`, key stored in
  `--keyfile`). Attributability requires a *stable* key, so it's generated once and reused.
- **Dry-run is the default.** `--live` is required to publish.

## How it reads

Reads via `buzz messages get --channel <UUID> --kinds 9` — the authenticated CLI path.
The agent authenticates **as itself** (a channel member); it does **not** hold the
owner's private key. Publishing to the public Bitchat relays uses the separate bridge key.

## How it picks relays (the fix)

Bitchat geohash channels do **not** use a fixed relay list. When a client teleports to a
geohash it subscribes to the ~5 relays **geographically nearest the cell center**, chosen
from a directory it fetches at runtime
(`permissionlesstech/georelays/nostr_relays.csv`) — see `GeoRelayDirectory.closestRelays`.
Publishing to a fixed set (damus/nos.lol/primal/offchain) meant events were accepted and
retrievable on *our* relays but invisible on the phone, which listened to a different set.

The bridge now replicates that selection: it decodes the geohash center, ranks the
directory relays by haversine distance (union of the remote directory + bundled fallback),
and publishes to the `--relay-count` (default 8) nearest — a superset of the phone's
nearest-5, with margin for directory drift. `--no-remote` uses only the bundled CSV.

## Usage — mirror (`bridge.py`)

```bash
# Dry-run (default). --preview N renders the exact kind-20000 payload for the N latest msgs.
python3 bridge.py --preview 3

# Repeatable preview without touching state:
python3 bridge.py --preview 3 --no-save

# Go live (publishes NEW messages to the Bitchat relays):
python3 bridge.py --live

# Go live + re-broadcast the latest announcement to widen the ephemeral catch window:
python3 bridge.py --live --rebroadcast --rebroadcast-interval 120 --rebroadcast-window 600
```

Poll on an interval (e.g. cron / `while sleep 60`) to forward new announcements as they land.

Key flags: `--channel`, `--geohash`, `--relay-csv`, `--relay-count`, `--no-remote`,
`--label`, `--prefix`, `--limit`, `--keyfile`, `--state`, `--live`, `--no-save`,
`--preview N`, `--rebroadcast`, `--rebroadcast-interval`, `--rebroadcast-window`.

## Behavior

- **Cold start** (empty state) absorbs all existing history into the seen-set and forwards
  nothing — only messages that arrive *after* the first run are forwarded.
- **Dedupe by event id** via the state file, so a message is never forwarded twice.
- **Re-broadcast** (`--rebroadcast`, LIVE only): `kind-20000` events are *ephemeral* —
  relays only serve them for a few minutes, so a phone that joins the geohash late misses
  the announcement. When enabled, each new announcement is re-emitted as a **fresh** event
  (new id + `created_at` → fresh retention window) every `--rebroadcast-interval` seconds
  for `--rebroadcast-window` seconds (default: every 120s for 600s ≈ 5–6 emits). A newer
  announcement mid-window re-arms the timer to the latest message. Re-broadcast state lives
  under `"rebroadcast"` in the state file.

Relays (publish targets): computed per-geohash — the nearest relays to the destination
cell center (see "How it picks relays" above), NOT a fixed list.

## Translate-then-forward (`translate_forward.py`)

Same rails as the mirror, plus a translation hop. One announcement is rendered into N
language variants and each is forwarded to the geohash tagged `["l", <lang>]`. The tag is
carried on each variant so a client *could* show a reader only their own language — but
Bitchat does **not** filter on the `l` tag today, so every client currently sees all
variants. Per-language filtering is a proposed client change that nobody has built yet.

```
Buzz announcement (kind-9)
    │
    ▼
Langlayer  POST {LANGLAYER_URL}/v1/render   →  [ (en, …), (es, …), (zh, …) ]
    │   FAIL-OPEN: on any error the ORIGINAL text is still emitted (Langlayer's
    │              Tier-4 floor — "never lose the original"), so nothing is dropped.
    ▼
one signed kind-20000 per variant  →  the geo-nearest relays (reuses bridge.py's selection)
```

**This now runs against the live endpoint — the offline stub is retired as the operating
mode.** Earlier this prototype translated with a deterministic offline stub (every line
read `offline-stub/stub q=0.50`). Langlayer's stateless `/v1/render` has since shipped
(CI green), and `translate_forward.py` runs against it directly: pass
`--langlayer-url https://langlayer.onrender.com` (real providers). The stub survives only
as the automatic **fail-open** path if the endpoint is unreachable — never as the default.

Confirmed live, preview-only, against `https://langlayer.onrender.com` for
*"Water and a charging station are available in the community shelter."*:

```
es  →  Agua y una estación de carga están disponibles en el refugio comunitario.
zh  →  社区避难所提供饮用水和充电站。
```

(`provider: ai-realtime`, `untranslated: false`, `quality_estimate ≈ 0.94` — genuine model
output, no simulator prefix.)

```bash
# Preview the per-language variants for the latest announcements (nothing published):
python3 translate_forward.py --langlayer-url https://langlayer.onrender.com \
    --languages en es zh --preview

# Go live — forward each variant to the geohash:
python3 translate_forward.py --langlayer-url https://langlayer.onrender.com \
    --languages en es zh --live
```

With no `--langlayer-url` (and no `LANGLAYER_URL` env) it uses the offline stub — handy for
running the demo with no server, but the output is simulated, not translated.

Key flags: `--channel`, `--geohash`, `--languages`, `--source-language`, `--langlayer-url`
(or `LANGLAYER_URL`), `--relay-csv`, `--relay-count`, `--no-remote`, `--label`, `--limit`,
`--keyfile`, `--state`, `--preview`, `--live`, `--no-save`.

## Staying online (durable runner)

The forwarder is a *poller* — each run reads new announcements and forwards them, so
"staying online" needs a supervisor loop that never dies. Plain background loops get
**reaped** (~1 min) and don't survive a reboot, so `runner.py` is the real answer: a
crash-proof forward loop **plus a public status page** anyone can open to watch it work:

- `/` — live dashboard (state, uptime, last activity, next run, geohash, languages, Langlayer URL)
- `/status` — JSON, `/healthz` — health probe

No credentials are needed to *view* it; the loop catches per-iteration errors and keeps
going; it fails fast with a clear message if creds are missing. It binds `0.0.0.0:$PORT`
(hosts inject `$PORT`; defaults to `8787`).

All three read env for both creds and behavior:

| Purpose | Vars |
|---------|------|
| Credentials (read side, `buzz` CLI) | `BUZZ_RELAY_URL`, `BUZZ_PRIVATE_KEY` (bot key), `BUZZ_AUTH_TAG` |
| Behavior | `LANGLAYER_URL`, `GEOHASH`, `LANGUAGES`, `SOURCE_LANGUAGE`, `INTERVAL`, `RELAY_COUNT`, `PORT` |

### Read-side credential gate (both paths)

Both paths need the same read credential before a single forward can happen. The runner
reads the channel with the **bot** identity, and Buzz relay reads require a **NIP-OA
owner-attestation auth tag** (`BUZZ_AUTH_TAG`) *per request* — the bot key alone returns
`403 relay_membership_required`. That tag is minted via Buzz **Desktop agent-provisioning**
(bounded with a `created_at<` clause and scoped with `kind=` clauses to only what the bridge
reads, since an issued tag can't be revoked). Until it is present in the runner's env, the
runner boots but 403s on reads — on Render and on your Mac alike.

### Path A — local launchd (durable, private)

Runs whenever your Mac is on; survives logout **and** reboot. `buzz` and the creds are
already local, so it's the low-friction path — but "viewable by others" only if you expose
the port via a tunnel, so in practice it's the *private* durable option.

```bash
cd ~/.buzz/REPOS/buzz-ops-bridge
cp deploy/bridge.env.example .secrets/bridge.env   # fill in real values, then:
chmod 600 .secrets/bridge.env
# edit the /ABSOLUTE/PATH placeholders in the plist to this repo's path:
cp deploy/com.buzzbridge.runner.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.buzzbridge.runner.plist
open http://127.0.0.1:8787      # the status dashboard
```

Full Path A kit and details live in `DEPLOY.md`, in the separate bridge repo.

### Path B — hosted Render Web Service (always-online + public URL)

The one that matches "up 24/7 and **anyone** can open a link and watch." Same `runner.py`,
run as a **Web Service** beside Langlayer on Render. The repo ships a multi-stage
`Dockerfile` that compiles the real Apache-2.0 `buzz` CLI from source (`block/buzz`,
`cargo build -p buzz-cli --release`, pinned commit) into a slim Rust stage, then copies the
binary into a Python runtime with the bridge. Render builds the image directly from the repo.

The bridge implementation Render builds — the `Dockerfile`, `requirements.txt` (coincurve +
websockets, manylinux wheels), `bridge.py`, `translate_forward.py`, `runner.py`, the
`online_relays_gps.csv` geo-relay directory, and `DEPLOY.md` — lives in a **separate
repo**, not in this one. Point Render at that repo as the build source. Keep the `BUZZ_*`
secrets out of the image: the three values go in as Render **environment variables /
secrets**, never baked into the build. Set `LANGLAYER_URL=https://langlayer.onrender.com`
and the behavior vars above; Render injects `$PORT`.

The read-side credential gate above applies here too — the container builds and boots
without `BUZZ_AUTH_TAG`, but 403s on reads until it's set in Render's env.
