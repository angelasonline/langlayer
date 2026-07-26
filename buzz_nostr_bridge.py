"""Buzz -> Language Layer bridge. Live streaming, done right per the relay source.

Per crates/buzz-relay: an authenticated REQ with a valid #h channel UUID
registers a channel-scoped subscription and calls retain_topic(channel), which
subscribes this connection to that channel's live fan-out. The relay then sends
historical matches followed by EOSE, and pushes NEW events on the SAME
subscription as they arrive.

Correct client behavior (the part earlier versions got wrong):
  - authenticate, send ONE REQ with #h + kinds, and NEVER resubscribe
    (resubscribing calls release_topic and drops live fan-out).
  - after EOSE keep reading forever on the same socket; new EVENT frames are
    live messages.
  - a websocket ping keepalive holds the connection without touching the sub.

Setup:
    export BUZZ_NSEC=nsec1...
    export BUZZ_RELAY=wss://<community>.communities.buzz.xyz
    export BUZZ_CHANNEL=<channel h uuid>
    export LL_BASE_URL=https://langlayer.onrender.com
    export LL_ACCESS_CODE=LL-XXXX
    python3 buzz_nostr_bridge.py
"""
from __future__ import annotations
import asyncio, json, os, time, urllib.request
import websockets
from pynostr.event import Event
from pynostr.key import PrivateKey

RELAY=os.environ.get("BUZZ_RELAY","")
NSEC=os.environ.get("BUZZ_NSEC","")
CHANNEL=os.environ.get("BUZZ_CHANNEL","")
LL_BASE=os.environ.get("LL_BASE_URL","http://localhost:8000").rstrip("/")
ACCESS_CODE=os.environ.get("LL_ACCESS_CODE","")
PREFIX=os.environ.get("BUZZ_PREFIX","\U0001F4E2")

def _get(p):
    with urllib.request.urlopen(f"{LL_BASE}{p}",timeout=30) as r: return json.load(r)
def _post(p,b):
    req=urllib.request.Request(f"{LL_BASE}{p}",data=json.dumps(b).encode(),
        headers={"Content-Type":"application/json"},method="POST")
    with urllib.request.urlopen(req,timeout=60) as r: return json.load(r)

def ll_channel():
    if not ACCESS_CODE: raise SystemExit("Set LL_ACCESS_CODE")
    return _get(f"/v1/events/{ACCESS_CODE}")["channel_id"]

def deliver(cid,text):
    body=text[len(PREFIX):].strip() if PREFIX and text.startswith(PREFIX) else text
    r=_post(f"/v1/channels/{cid}/events",
        {"channel_id":cid,"priority_class":"announcement","kind":"text","payload":body})
    rec=r.get("receipts",[]); dl=sum(1 for x in rec if x.get("delivered"))
    try: langs=_get(f"/v1/events/{ACCESS_CODE}/summary").get("languages",[])
    except Exception: langs=[]
    tail=f" across {len(langs)} language(s): {', '.join(langs)}" if langs else ""
    print(f"  -> delivered to {dl}/{len(rec)} attendee(s){tail}")

def auth_msg(pk,ch):
    e=Event(kind=22242,content="",pubkey=pk.public_key.hex())
    e.add_tag("relay",RELAY); e.add_tag("challenge",ch)
    e.created_at=int(time.time()); e.compute_id(); e.sign(pk.hex())
    return json.dumps(["AUTH",e.to_dict()])

def req_msg():
    # channel-scoped sub: #h with the UUID triggers retain_topic(channel) on the relay.
    # no 'since' so historical returns; live events push on the same sub after EOSE.
    f={"kinds":[9]}
    if CHANNEL: f["#h"]=[CHANNEL]
    return json.dumps(["REQ","bridge",f])

async def run():
    if not(NSEC and RELAY): raise SystemExit("Set BUZZ_NSEC and BUZZ_RELAY")
    cid=ll_channel(); pk=PrivateKey.from_nsec(NSEC)
    print(f"identity: {pk.public_key.bech32()[:22]}...")
    print(f"relay:    {RELAY}")
    print(f"channel:  {CHANNEL or '(all)'}")
    print(f"space:    {ACCESS_CODE}, forwarding messages starting with {PREFIX!r}")
    seen=set()
    while True:  # reconnect only on real disconnect
        try:
            async with websockets.connect(RELAY,max_size=2**20,
                                          ping_interval=20,ping_timeout=20) as ws:
                await ws.send(req_msg())     # ONE req; never resent
                subscribed=False; priming=True
                while True:
                    try:
                        raw=await asyncio.wait_for(ws.recv(),timeout=30)
                    except asyncio.TimeoutError:
                        await ws.ping()       # keepalive, does not touch the sub
                        continue
                    m=json.loads(raw); t=m[0]
                    if t=="AUTH":
                        await ws.send(auth_msg(pk,m[1]))
                        if not subscribed:
                            await ws.send(req_msg())  # first real sub, post-auth
                            subscribed=True
                    elif t=="EOSE":
                        priming=False
                        print("ready; watching live. Post a "
                              f"{PREFIX} message in the channel.\n")
                    elif t=="EVENT":
                        ev=m[2]; eid=ev.get("id",""); text=ev.get("content","") or ""
                        if not eid or eid in seen: continue
                        seen.add(eid)
                        if priming: continue          # existing history: record only
                        if PREFIX and not text.startswith(PREFIX): continue
                        print(f"buzz {eid[:8]}: {text[:60]}")
                        try: deliver(cid,text)
                        except Exception as e: print(f"  !! delivery error: {e}")
                    elif t in ("NOTICE","CLOSED"):
                        pass
        except (websockets.ConnectionClosed,OSError) as e:
            print(f"reconnecting ({type(e).__name__})...")
            await asyncio.sleep(2)

if __name__=="__main__":
    try: asyncio.run(run())
    except KeyboardInterrupt: print("\nstopped.")
