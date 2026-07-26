# buzz-ops-bridge

Mirrors the Buzz **announcements** channel to a Nostr **geohash** channel (Bitchat's
location-channel format) so announcements can be received on a second, resilient path.

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

## Usage

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

## Running it hands-off (60s supervisor)

`.scratch/cron-supervisor.sh` runs `bridge.py --live --rebroadcast` every 60s. It inherits
the live session credentials **in memory** (nothing secret written to disk) rather than
using system `crontab`, which would require the managed private key on disk. It survives
the session ending but **not** a machine reboot.

```bash
# start
cd ~/.buzz/REPOS/buzz-ops-bridge && nohup zsh .scratch/cron-supervisor.sh >/dev/null 2>&1 &

# status (is it alive? + recent forwards / RE-BROADCAST lines)
ps -p "$(cat .scratch/bridge-cron.pid)" -o pid,etime,command
tail -30 .scratch/bridge-cron.log

# stop
kill "$(cat .scratch/bridge-cron.pid)"
```
