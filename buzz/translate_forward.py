#!/usr/bin/env python3
"""translate_forward — Buzz → Langlayer → Bitchat "translate-then-forward" bridge.

This is the prototype for the integration scoped in the Fizz/Angela DM: an organizer
posts ONE announcement in Buzz; Langlayer renders it into N languages/formats; each
variant is forwarded to Bitchat's geohash channel so phones in a place receive the
message in their own language — even when the usual channels are down.

Pipeline
--------
    Buzz announcements channel (kind-9, read via `buzz messages get`)
        │
        ▼
    LanglayerClient.render(text, targets)   →  [Variant(lang, modality, content), ...]
        │   • HTTP mode: POST {LANGLAYER_URL}/v1/render   (LIVE — see below)
        │   • Offline mode: deterministic labeled stub, so the demo runs with no server
        │   • FAIL-OPEN: on any error we still emit the ORIGINAL text (Langlayer Tier-4:
        │                "never lose the original"), so nothing is ever dropped.
        ▼
    one signed kind-20000 per variant, tagged ["g", geohash] + ["l", lang] + ["n", label]
        │
        ▼
    published to the N relays geographically nearest the geohash — the exact set the
    phone subscribes to (reuses bridge.select_relays, the fix that made 1r23b work).

The endpoint this needs (now shipped)
-------------------------------------
Langlayer's per-*attendee* API (POST /v1/channels/{cid}/events, read back via the
transcript) is stateful and assumes attendees have joined for every language. A
transport bridge like this one needs a STATELESS "render these N languages for this
payload" call that returns the artifacts directly. That endpoint — /v1/render — has
since shipped, so this client runs against the live endpoint: point --langlayer-url
(or LANGLAYER_URL) at a real instance, e.g. https://langlayer.onrender.com. The
offline stub is no longer the operating mode; it remains only as the automatic
fail-open path when no URL is set or the endpoint is unreachable. See
LANGLAYER_INTEGRATION.md for the full change list.

Reuses the proven machinery in bridge.py (geohash decode, geo-nearest relay selection,
schnorr signing, publish, seen-state) rather than duplicating it.

Only stdlib + `coincurve` + `websockets` are required (same as bridge.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Reuse the battle-tested pieces from the sibling forwarder.
from bridge import (
    build_event,
    fetch_history,
    load_or_create_bridge_key,
    load_relay_directory,
    load_state,
    publish,
    pubkey_hex,
    save_state,
    select_relays,
)


# --------------------------------------------------------------------- Langlayer
@dataclass
class Variant:
    """One rendered output — mirrors Langlayer's Artifact (see langlayer/providers.py)."""
    language: str
    modality: str
    content: str
    provider: str = "offline-stub"
    quality_estimate: float = 0.0
    source_used: str = "stub"


@dataclass
class Target:
    language: str
    modality: str = "text"


# A tiny, honest offline dictionary. This is NOT machine translation — it is a
# deterministic stand-in (exactly like Langlayer's own "simulated provider until
# vendors implement") so the forwarding path is demonstrable with no server running.
# In production every Variant.content comes from Langlayer's real providers.
_STUB_BANNERS = {
    "en": "EN",
    "es": "ES · Español",
    "zh": "ZH · 中文",
    "fr": "FR · Français",
    "ar": "AR · العربية",
    "vi": "VI · Tiếng Việt",
    "ht": "HT · Kreyòl",
}
_STUB_PHRASES = {
    # Just enough real vocabulary to make a live demo legible; falls through to a
    # transparent "[stub <lang>]" prefix for anything not hand-seeded.
    ("Shelter open at the community center. Water and charging available.", "es"):
        "Refugio abierto en el centro comunitario. Hay agua y carga de dispositivos.",
    ("Shelter open at the community center. Water and charging available.", "zh"):
        "社区中心已开放避难所。提供饮水和充电。",
    ("Shelter open at the community center. Water and charging available.", "fr"):
        "Abri ouvert au centre communautaire. Eau et recharge disponibles.",
}


class LanglayerClient:
    """Renders a source string into N language/modality variants.

    Tries the (proposed) Langlayer HTTP endpoint first; on ANY failure returns None
    so the caller fails open to the original text. Offline mode uses the stub.
    """

    def __init__(self, base_url: str | None, source_language: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/") if base_url else None
        self.source_language = source_language
        self.timeout = timeout

    # -- HTTP mode against the PROPOSED POST /v1/render ------------------------
    def _render_http(self, text: str, targets: list[Target]) -> list[Variant] | None:
        body = json.dumps({
            "payload": text,
            "source_language": self.source_language,
            "priority_class": "announcement",
            "targets": [{"language": t.language, "modality": t.modality} for t in targets],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/render",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001 - any failure => fail open
            print(f"  langlayer   : HTTP render failed ({e}); using offline stub")
            return None
        out = []
        for v in data.get("variants", []):
            out.append(Variant(
                language=v.get("language", "?"),
                modality=v.get("modality", "text"),
                content=v.get("content", ""),
                provider=v.get("provider", "langlayer"),
                quality_estimate=float(v.get("quality_estimate", 0.0)),
                source_used=v.get("source_used", "cloud"),
            ))
        return out or None

    # -- Offline stub ---------------------------------------------------------
    def _render_stub(self, text: str, targets: list[Target]) -> list[Variant]:
        out = []
        for t in targets:
            if t.language == self.source_language:
                content = text  # source language passes through verbatim
            else:
                content = _STUB_PHRASES.get((text, t.language))
                if content is None:
                    content = f"[stub {t.language}] {text}"
            out.append(Variant(
                language=t.language,
                modality=t.modality,
                content=content,
                provider="offline-stub",
                quality_estimate=0.5,
                source_used="stub",
            ))
        return out

    def render(self, text: str, targets: list[Target]) -> list[Variant]:
        text = (text or "").strip()
        variants = None
        if self.base_url:
            variants = self._render_http(text, targets)
        if variants is None:
            variants = self._render_stub(text, targets)
        return variants


def variant_banner(lang: str) -> str:
    return _STUB_BANNERS.get(lang, lang.upper())


# ------------------------------------------------------------- variant -> event
def build_variant_event(bridge_sk, geohash: str, label: str, v: Variant) -> dict:
    """One kind-20000 per variant.

    Tags:
      ["g", geohash]  — Bitchat geohash channel routing (unchanged).
      ["l", lang]     — language tag. Bitchat ignores unknown tags today, but a
                        language-aware client can filter the channel to the user's
                        chosen language (see the Bitchat-side note in the writeup).
      ["n", label]    — attributable bridge name.
    The banner is ALSO written into the content so it is legible on today's clients
    that don't yet read the `l` tag.
    """
    banner = variant_banner(v.language)
    # Include the modality in the banner + tags so a language passthrough at two
    # modalities (e.g. en/text vs en/simplified) stays DISTINCT — same content +
    # same tags would otherwise hash to one event id and silently collapse.
    label_suffix = "" if v.modality == "text" else f" · {v.modality}"
    content = f"[{banner}{label_suffix}] {v.content}"
    tags = [["g", geohash], ["l", v.language], ["m", v.modality], ["n", label]]
    return build_event(bridge_sk, 20000, tags, content)


# ------------------------------------------------------------------------- run
async def main_async(args) -> int:
    keyfile = Path(args.keyfile)
    bridge_sk = load_or_create_bridge_key(keyfile)

    targets = [Target(*_parse_target(t)) for t in args.languages]
    client = LanglayerClient(args.langlayer_url, args.source_language)

    state_path = Path(args.state)
    state = load_state(state_path)
    seen = set(state.get("seen", []))
    cold_start = not seen

    mode = "LIVE" if args.live else "DRY-RUN"
    directory = load_relay_directory(Path(args.relay_csv), use_remote=not args.no_remote)
    publish_relays = select_relays(directory, args.geohash, args.relay_count)

    print("=" * 72)
    print(f"translate-forward  [{mode}]")
    print(f"  read source     : buzz messages get --channel {args.channel} (kind 9)")
    print(f"  langlayer       : {args.langlayer_url or '(offline stub — no server)'}")
    print(f"  source language : {args.source_language}")
    print(f"  target variants : {', '.join(f'{t.language}/{t.modality}' for t in targets)}")
    print(f"  publish key     : {pubkey_hex(bridge_sk)}  (label='{args.label}')")
    print(f"  destination     : geohash '{args.geohash}'")
    print(f"  publish relays  : {len(publish_relays)} nearest to '{args.geohash}' (phone's set)")
    print("=" * 72)

    try:
        history = fetch_history(args.channel, args.limit)
    except Exception as e:  # noqa: BLE001
        print(f"ERROR reading announcements channel: {e}", file=sys.stderr)
        return 2
    history.sort(key=lambda e: e.get("created_at", 0))
    print(f"Read {len(history)} kind-9 message(s) from history.\n")

    if cold_start:
        for m in history:
            seen.add(m["id"])
        print(f"COLD START: absorbed {len(history)} existing message(s); none forwarded.\n")
        forward_msgs = []
    else:
        forward_msgs = [m for m in history if m["id"] not in seen]

    # Optional preview of the newest history message so the owner sees the exact
    # multi-variant payload WITHOUT forwarding or marking anything seen.
    if args.preview and history:
        m = history[-1]
        print("PREVIEW — exact variants for the most recent message "
              "(NOT forwarded):\n")
        await _render_and_show(client, targets, bridge_sk, args, m, live=False)
        print()

    if not forward_msgs:
        if not cold_start:
            print("No new messages to forward.\n")
    else:
        print(f"{'PUBLISHING' if args.live else 'WOULD PUBLISH'} "
              f"{len(forward_msgs)} new message(s), each as "
              f"{len(targets)} variant(s):\n")
        for m in forward_msgs:
            await _render_and_show(client, targets, bridge_sk, args, m,
                                   live=args.live, relays=publish_relays)
            seen.add(m["id"])
            print()

    if not args.no_save:
        state["seen"] = sorted(seen)
        save_state(state_path, state)
        print(f"State saved: {len(seen)} known id(s) -> {state_path}")
    else:
        print("(--no-save: state NOT written)")

    print(f"\nDone. Mode was {mode}.")
    return 0


async def _render_and_show(client, targets, bridge_sk, args, msg, live, relays=None):
    src = (msg.get("content") or "").strip()
    print(f"  source kind-9 id : {msg['id'][:16]}…")
    print(f"  source content   : {src!r}")
    variants = client.render(src, targets)
    for v in variants:
        evt = build_variant_event(bridge_sk, args.geohash, args.label, v)
        tag = f"{v.provider}/{v.source_used} q={v.quality_estimate:.2f}"
        print(f"    → [{variant_banner(v.language)}] {evt['id'][:12]}…  ({tag})")
        print(f"        {evt['content'][:100]!r}")
        if live and relays is not None:
            res = await publish(relays, evt)
            for url, r in res.items():
                print(f"        {url}: {r}")


def _parse_target(spec: str) -> tuple[str, str]:
    """'es' -> ('es','text');  'en:simplified' -> ('en','simplified')."""
    if ":" in spec:
        lang, modality = spec.split(":", 1)
        return lang.strip(), modality.strip()
    return spec.strip(), "text"


def parse_args(argv):
    p = argparse.ArgumentParser(
        description="translate-forward: Buzz -> Langlayer -> Bitchat geohash mirror")
    p.add_argument("--channel", default="d7d80567-5b68-41ef-b7b9-31d8c34d321f",
                   help="Buzz announcements channel UUID to read (kind 9)")
    p.add_argument("--geohash", default="1r23b",
                   help="destination geohash (default 1r23b = Point Nemo, no residents)")
    p.add_argument("--languages", nargs="+",
                   default=["en", "es", "zh"],
                   metavar="LANG[:MODALITY]",
                   help="target variants, e.g. en es zh en:simplified ht")
    p.add_argument("--source-language", default="en",
                   help="language of the Buzz announcements (source)")
    p.add_argument("--langlayer-url", default=os.environ.get("LANGLAYER_URL"),
                   help="Langlayer base URL (e.g. http://localhost:8000). "
                        "If unset, uses the offline stub translator.")
    p.add_argument("--relay-csv", default=".scratch/online_relays_gps.csv",
                   help="bundled georelay directory fallback CSV")
    p.add_argument("--relay-count", type=int, default=8,
                   help="publish to the N nearest relays")
    p.add_argument("--no-remote", action="store_true",
                   help="do not fetch the remote georelay directory; use bundled CSV only")
    p.add_argument("--label", default="langlayer-bridge", help="n tag label")
    p.add_argument("--limit", type=int, default=200, help="history fetch limit")
    p.add_argument("--preview", action="store_true",
                   help="show variants for the newest message without forwarding")
    p.add_argument("--keyfile", default=".scratch/langlayer-bridge.key",
                   help="persistent publishing key (hex)")
    p.add_argument("--state", default=".scratch/langlayer-bridge-state.json",
                   help="dedupe/seen state file")
    p.add_argument("--live", action="store_true",
                   help="ACTUALLY publish to Bitchat relays (default: dry-run)")
    p.add_argument("--no-save", action="store_true",
                   help="do not write state (safe for repeated previews)")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async(parse_args(sys.argv[1:]))))
