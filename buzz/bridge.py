#!/usr/bin/env python3
"""buzz-ops-bridge — mirror a Buzz announcements channel to a Nostr geohash channel.

Reads kind-9 messages from the Buzz announcements channel via the authenticated
`buzz messages get` CLI (the same query pattern used to read the channel earlier —
the agent authenticates as itself, a channel member; it does NOT hold the owner's
private key) and re-publishes each NEW message as a kind-20000 ephemeral event tagged
["g", <geohash>] + ["n", <label>], content prefixed with a neutral banner.

Design points that make this a SELF-MIRROR (not a broadcast to strangers):
  * Destination geohash defaults to s0000 (open-ocean cell, no residents). The owner
    teleports their own Bitchat client to that cell to receive.
  * Publishing key is LABELED and PERSISTENT (stored in --keyfile). An attributable
    identity must reuse the same key across runs, so we generate once and reuse.
  * Dry-run is the default. --live is required to actually publish, and even then the
    owner gates go-live.

Only stdlib + `coincurve` (schnorr) + `websockets` (asyncio) are required.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import websockets
from coincurve import PrivateKey

# DEPRECATED fixed relay set. Bitchat does NOT use a fixed relay list for geohash
# channels — it picks the relays geographically nearest the geohash center (see
# select_relays below). Publishing to this fixed set caused announcements to be
# accepted+retrievable on OUR relays yet invisible on the phone, which subscribes to
# a DIFFERENT, geo-nearest set. Kept only as a last-resort fallback if no directory
# CSV is available. Do not publish geohash events here.
BITCHAT_RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.primal.net",
    "wss://offchain.pub",
]

# The same remote directory Bitchat's GeoRelayDirectory fetches at runtime. The phone
# selects its subscription relays from THIS list, so the bridge must too.
REMOTE_RELAY_CSV = ("https://raw.githubusercontent.com/permissionlesstech/"
                    "georelays/refs/heads/main/nostr_relays.csv")

BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


# ---------------------------------------------------------------- geohash routing
def geohash_decode_center(gh: str) -> tuple[float, float]:
    """Decode a geohash to its center (lat, lon) — matches Bitchat's Geohash.decodeCenter."""
    lat = (-90.0, 90.0)
    lon = (-180.0, 180.0)
    is_lon = True
    for c in gh.strip().lower():
        cd = BASE32.index(c)
        for mask in (16, 8, 4, 2, 1):
            if is_lon:
                mid = (lon[0] + lon[1]) / 2
                lon = (mid, lon[1]) if cd & mask else (lon[0], mid)
            else:
                mid = (lat[0] + lat[1]) / 2
                lat = (mid, lat[1]) if cd & mask else (lat[0], mid)
            is_lon = not is_lon
    return (lat[0] + lat[1]) / 2, (lon[0] + lon[1]) / 2


def _haversine_km(la1: float, lo1: float, la2: float, lo2: float) -> float:
    import math
    dla = math.radians(la2 - la1)
    dlo = math.radians(lo2 - lo1)
    a = (math.sin(dla / 2) ** 2
         + math.cos(math.radians(la1)) * math.cos(math.radians(la2)) * math.sin(dlo / 2) ** 2)
    return 6371.0 * 2 * math.atan2(a ** 0.5, (1 - a) ** 0.5)


def parse_relay_csv(text: str) -> list[tuple[str, float, float]]:
    """Parse the georelay CSV (host,lat,lon) — mirrors GeoRelayDirectory.parseCSV."""
    out: list[tuple[str, float, float]] = []
    for i, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line:
            continue
        if i == 0 and "relay url" in line.lower():
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            lat, lon = float(parts[1]), float(parts[2])
        except ValueError:
            continue
        out.append((parts[0], lat, lon))
    return list(dict.fromkeys(out))


def load_relay_directory(csv_path: Path, use_remote: bool = True) -> list[tuple[str, float, float]]:
    """Union of the remote directory (what the phone uses) and the bundled fallback.

    Publishing to the union guarantees we cover the phone's nearest-N selection even
    when the remote and bundled directories disagree on the tail entries.
    """
    entries: list[tuple[str, float, float]] = []
    if use_remote:
        try:
            import urllib.request
            with urllib.request.urlopen(REMOTE_RELAY_CSV, timeout=20) as r:
                entries = parse_relay_csv(r.read().decode("utf-8"))
            print(f"  relay directory : {len(entries)} relays from remote (phone's source)")
        except Exception as e:  # noqa: BLE001
            print(f"  relay directory : remote fetch failed ({e}); using bundled only")
    bundled: list[tuple[str, float, float]] = []
    if csv_path.exists():
        bundled = parse_relay_csv(csv_path.read_text())
    # Union, remote first (authoritative), then any bundled-only extras.
    seen = {e[0] for e in entries}
    for e in bundled:
        if e[0] not in seen:
            entries.append(e)
            seen.add(e[0])
    return entries


def select_relays(entries: list[tuple[str, float, float]], geohash: str, count: int) -> list[str]:
    """Bitchat's closestRelays: nearest `count` relays to the geohash center, ties by host."""
    if not entries:
        return list(BITCHAT_RELAYS)  # last-resort fallback
    lat, lon = geohash_decode_center(geohash)
    ranked = sorted(entries, key=lambda e: (_haversine_km(lat, lon, e[1], e[2]), e[0]))
    return [f"wss://{h}" for (h, _la, _lo) in ranked[:count]]


# --------------------------------------------------------------------------- keys
def _bech32_decode(bech: str) -> bytes:
    """Minimal bech32 decode -> raw data bytes (for nsec keys)."""
    bech = bech.strip().lower()
    pos = bech.rfind("1")
    data = bech[pos + 1:]
    vals = [BECH32_CHARSET.find(c) for c in data]
    if any(v == -1 for v in vals):
        raise ValueError("invalid bech32 character")
    vals = vals[:-6]  # drop 6-char checksum
    # 5-bit -> 8-bit
    acc = 0
    bits = 0
    out = bytearray()
    for v in vals:
        acc = (acc << 5) | v
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xFF)
    return bytes(out)


def load_privkey(raw: str) -> PrivateKey:
    raw = raw.strip()
    if raw.startswith("nsec1"):
        return PrivateKey(_bech32_decode(raw))
    if len(raw) == 64:
        return PrivateKey(bytes.fromhex(raw))
    raise ValueError("private key must be 64-char hex or nsec1...")


def pubkey_hex(sk: PrivateKey) -> str:
    """x-only (BIP340 / Nostr) public key as 32-byte hex."""
    return sk.public_key.format(compressed=True)[1:].hex()


def load_or_create_bridge_key(keyfile: Path) -> PrivateKey:
    """Persistent, attributable publishing identity — generate once, reuse forever."""
    if keyfile.exists():
        return PrivateKey(bytes.fromhex(keyfile.read_text().strip()))
    sk = PrivateKey()
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text(sk.to_hex())
    os.chmod(keyfile, 0o600)
    return sk


# ------------------------------------------------------------------------- events
def _serialize(pub: str, created_at: int, kind: int, tags: list, content: str) -> str:
    return json.dumps(
        [0, pub, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def build_event(sk: PrivateKey, kind: int, tags: list, content: str,
                created_at: int | None = None) -> dict:
    pub = pubkey_hex(sk)
    created_at = created_at if created_at is not None else int(time.time())
    ser = _serialize(pub, created_at, kind, tags, content)
    eid = hashlib.sha256(ser.encode("utf-8")).hexdigest()
    sig = sk.sign_schnorr(bytes.fromhex(eid)).hex()
    return {
        "id": eid,
        "pubkey": pub,
        "created_at": created_at,
        "kind": kind,
        "tags": tags,
        "content": content,
        "sig": sig,
    }


def build_profile_event(sk: PrivateKey, label: str, geohash: str) -> dict:
    """One-time kind-0 metadata so the bridge shows a readable name in clients."""
    meta = {
        "name": label,
        "display_name": label,
        "about": (
            f"Attributable mirror of the Buzz announcements channel into geohash "
            f"'{geohash}'. Automated, one-way, self-hosted bridge."
        ),
    }
    content = json.dumps(meta, separators=(",", ":"), ensure_ascii=False)
    return build_event(sk, 0, [], content)


def make_transformer(bridge_sk: PrivateKey, geohash: str, label: str, prefix: str):
    def _t(msg: dict) -> dict:
        content = prefix + (msg.get("content") or "").strip()
        tags = [["g", geohash], ["n", label]]
        return build_event(bridge_sk, 20000, tags, content)
    return _t


# --------------------------------------------------------------------------- relay
def fetch_history(channel: str, limit: int) -> list[dict]:
    """Read kind-9 messages from the Buzz announcements channel via the CLI.

    Uses `buzz messages get`, which authenticates through the harness as this agent
    (a channel member). Returns the parsed list of message dicts.
    """
    cmd = ["buzz", "messages", "get", "--channel", channel,
           "--kinds", "9", "--limit", str(limit)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"`buzz messages get` failed (exit {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}")
    data = json.loads(proc.stdout or "[]")
    return [m for m in data if m.get("kind") == 9]


async def publish(relays: list[str], event: dict, timeout: float = 10.0) -> dict:
    """Publish one event to each relay; collect OK/errors. LIVE only."""
    results = {}
    for url in relays:
        try:
            async with websockets.connect(url, max_size=2 ** 22) as ws:
                await ws.send(json.dumps(["EVENT", event]))
                ok = "sent (no OK received)"
                try:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        m = json.loads(raw)
                        if m[0] == "OK" and m[1] == event["id"]:
                            ok = f"OK accepted={m[2]} {m[3] if len(m) > 3 else ''}".strip()
                            break
                except asyncio.TimeoutError:
                    pass
                results[url] = ok
        except Exception as e:  # noqa: BLE001 - report per-relay, keep going
            results[url] = f"ERROR: {e}"
    return results


# ---------------------------------------------------------------------------- state
def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"seen": []}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


# ----------------------------------------------------------------------------- run
def render_preview(orig: dict, evt: dict) -> str:
    body = orig.get("content", "")
    if len(body) > 120:
        body = body[:117] + "..."
    lines = [
        f"  source kind-9 id : {orig['id'][:16]}…  ({time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(orig.get('created_at', 0)))})",
        f"  source content   : {body!r}",
        f"  -> kind-20000 id : {evt['id'][:16]}…   signed by {evt['pubkey'][:16]}…",
        f"     tags          : {evt['tags']}",
        f"     content       : {evt['content'][:140]!r}",
    ]
    return "\n".join(lines)


async def main_async(args) -> int:
    keyfile = Path(args.keyfile)
    bridge_sk = load_or_create_bridge_key(keyfile)

    # One-shot: publish the kind-0 profile so the bridge shows a readable name,
    # then exit. Dry-run unless --live is set.
    if args.publish_profile:
        evt = build_profile_event(bridge_sk, args.label, args.geohash)
        mode = "LIVE" if args.live else "DRY-RUN"
        directory = load_relay_directory(Path(args.relay_csv), use_remote=not args.no_remote)
        publish_relays = select_relays(directory, args.geohash, args.relay_count)
        print("=" * 72)
        print(f"buzz-ops-bridge  [PROFILE / {mode}]")
        print(f"  publish key : {pubkey_hex(bridge_sk)}  (label='{args.label}')")
        print(f"  kind-0 id   : {evt['id']}")
        print(f"  content     : {evt['content']}")
        print(f"  relays      : {', '.join(publish_relays) if args.live else '(none — dry-run)'}")
        print("=" * 72)
        if args.live:
            res = await publish(publish_relays, evt)
            for url, r in res.items():
                print(f"  {url}: {r}")
        else:
            print("(dry-run — nothing published; re-run with --live)")
        return 0

    xform = make_transformer(bridge_sk, args.geohash, args.label, args.prefix)

    state_path = Path(args.state)
    state = load_state(state_path)
    seen = set(state.get("seen", []))
    cold_start = not seen  # first ever run absorbs history, forwards nothing

    mode = "LIVE" if args.live else "DRY-RUN"
    directory = load_relay_directory(Path(args.relay_csv), use_remote=not args.no_remote)
    publish_relays = select_relays(directory, args.geohash, args.relay_count)
    print("=" * 72)
    print(f"buzz-ops-bridge  [{mode}]")
    print(f"  read source     : buzz messages get --channel {args.channel} (kind 9)")
    print(f"  publish key     : {pubkey_hex(bridge_sk)}  (label='{args.label}')")
    print(f"                    stored at {keyfile}")
    print(f"  destination     : geohash '{args.geohash}'  prefix={args.prefix!r}")
    print(f"  publish relays  : {len(publish_relays)} nearest to '{args.geohash}' "
          f"(the phone's own set):")
    for u in publish_relays:
        print(f"                    {u}")
    print(f"  state file      : {state_path}  (known ids: {len(seen)})")
    print("=" * 72)

    try:
        history = fetch_history(args.channel, args.limit)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR reading announcements channel: {e}", file=sys.stderr)
        return 2
    history.sort(key=lambda e: e.get("created_at", 0))
    print(f"Read {len(history)} kind-9 message(s) from history.\n")

    # Determine what is NEW (not yet seen).
    new_msgs = [m for m in history if m["id"] not in seen]

    if cold_start:
        # Absorb ALL existing history so only future messages forward.
        for m in history:
            seen.add(m["id"])
        print(f"COLD START: absorbed {len(history)} existing message(s) into the "
              f"seen-set. None are forwarded.\n")
        forward_msgs = []
    else:
        forward_msgs = new_msgs

    # Optional preview: render the transform for the N most recent history msgs so
    # the owner can see the exact kind-20000 payload WITHOUT anything being published
    # or marked as forwarded.
    if args.preview and history:
        sample = history[-args.preview:]
        print(f"PREVIEW — exact kind-20000 payload for the {len(sample)} most recent "
              f"message(s).")
        print("(history samples; NOT forwarded — shown only so you can inspect format)\n")
        for m in sample:
            print(render_preview(m, xform(m)))
            print()

    # Forward genuinely-new messages.
    last_forward_evt = None
    if forward_msgs:
        print(f"{'PUBLISHING' if args.live else 'WOULD PUBLISH'} "
              f"{len(forward_msgs)} new message(s):\n")
        for m in forward_msgs:
            evt = xform(m)
            print(render_preview(m, evt))
            if args.live:
                res = await publish(publish_relays, evt)
                for url, r in res.items():
                    print(f"     {url}: {r}")
            seen.add(m["id"])
            last_forward_evt = evt
            print()
    else:
        if not cold_start:
            print("No new messages to forward.\n")

    # -------------------------------------------------------------- re-broadcast
    # Ephemeral kind-20000 events are only briefly retrievable from relays, so a
    # phone that joins the cell a few minutes late misses the announcement. To widen
    # the catch window we RE-EMIT the latest announcement as a fresh event
    # (new id + created_at => fresh retention window) every --rebroadcast-interval
    # seconds for --rebroadcast-window seconds after it first forwards.
    if args.rebroadcast and args.live:
        now = int(time.time())
        rb = state.get("rebroadcast") or {}
        if last_forward_evt is not None:
            # A new announcement just forwarded — that is re-broadcast emit #1.
            rb = {
                "active": True,
                "content": last_forward_evt["content"],
                "tags": last_forward_evt["tags"],
                "started_at": now,
                "last_emit": now,
                "emits": 1,
            }
            print(f"RE-BROADCAST armed: will re-emit every {args.rebroadcast_interval}s "
                  f"for {args.rebroadcast_window}s (emit #1 was the forward above).\n")
        elif rb.get("active"):
            elapsed = now - rb.get("started_at", now)
            since_last = now - rb.get("last_emit", 0)
            if elapsed > args.rebroadcast_window:
                rb["active"] = False
                print(f"RE-BROADCAST window closed after {rb.get('emits', 0)} emit(s) "
                      f"({elapsed}s elapsed). No further re-emits.\n")
            elif since_last >= args.rebroadcast_interval - 5:
                evt = build_event(bridge_sk, 20000, rb["tags"], rb["content"])
                rb["last_emit"] = now
                rb["emits"] = rb.get("emits", 0) + 1
                print(f"RE-BROADCAST emit #{rb['emits']} (elapsed {elapsed}s):")
                print(f"  -> kind-20000 id : {evt['id'][:16]}…  {evt['content'][:80]!r}")
                res = await publish(publish_relays, evt)
                for url, r in res.items():
                    print(f"     {url}: {r}")
                print()
            else:
                nxt = args.rebroadcast_interval - since_last
                print(f"RE-BROADCAST active: emit #{rb.get('emits', 0)} done, "
                      f"next in ~{max(nxt, 0)}s (elapsed {elapsed}s/{args.rebroadcast_window}s).\n")
        state["rebroadcast"] = rb

    if not args.no_save:
        state["seen"] = sorted(seen)
        save_state(state_path, state)
        print(f"State saved: {len(seen)} known id(s) -> {state_path}")
    else:
        print("(--no-save: state NOT written)")

    print(f"\nDone. Mode was {mode}.")
    return 0


def parse_args(argv):
    p = argparse.ArgumentParser(description="buzz-ops-bridge: Buzz -> Nostr geohash mirror")
    p.add_argument("--channel", default="d7d80567-5b68-41ef-b7b9-31d8c34d321f",
                   help="Buzz announcements channel UUID to read (kind 9)")
    p.add_argument("--geohash", default="1r23b",
                   help="destination geohash (g tag). Default 1r23b = Point Nemo, the "
                        "oceanic pole of inaccessibility (no residents). NOTE s0000 sits "
                        "beside null-island (0,0) where GPS-error clients congregate — "
                        "avoid it for a private demo.")
    p.add_argument("--relay-csv", default=".scratch/online_relays_gps.csv",
                   help="bundled georelay directory fallback CSV")
    p.add_argument("--relay-count", type=int, default=8,
                   help="publish to the N nearest relays (>=5 covers the phone's nearest-5 "
                        "with margin for remote/bundled directory drift)")
    p.add_argument("--no-remote", action="store_true",
                   help="do not fetch the remote georelay directory; use bundled CSV only")
    p.add_argument("--label", default="buzz-ops-bridge", help="n tag label")
    p.add_argument("--prefix", default="[Buzz announcement] ", help="content prefix")
    p.add_argument("--limit", type=int, default=200, help="history fetch limit")
    p.add_argument("--preview", type=int, default=0, metavar="N",
                   help="render exact payload for the N most recent history msgs")
    p.add_argument("--keyfile", default=".scratch/buzz-ops-bridge.key",
                   help="persistent publishing key (hex)")
    p.add_argument("--state", default=".scratch/bridge-state.json",
                   help="dedupe/seen state file")
    p.add_argument("--publish-profile", action="store_true",
                   help="one-shot: publish the kind-0 profile (name) then exit")
    p.add_argument("--live", action="store_true",
                   help="ACTUALLY publish to Bitchat relays (default: dry-run)")
    p.add_argument("--no-save", action="store_true",
                   help="do not write state (safe for repeated previews)")
    p.add_argument("--rebroadcast", action="store_true",
                   help="re-emit the latest announcement on a timer to widen the "
                        "ephemeral catch window (LIVE only)")
    p.add_argument("--rebroadcast-interval", type=int, default=120,
                   help="seconds between re-broadcast re-emits (default 120)")
    p.add_argument("--rebroadcast-window", type=int, default=600,
                   help="seconds to keep re-broadcasting after an announcement "
                        "(default 600 = 10 min)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    raise SystemExit(asyncio.run(main_async(args)))
