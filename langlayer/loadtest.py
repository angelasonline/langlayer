"""Load test: real HTTP + WebSocket clients against a running server.

Usage:
    uvicorn langlayer.api:app --port 8000          # terminal 1
    python loadtest.py --users 500 --url http://localhost:8000   # terminal 2

Simulates one community space: N attendees join over HTTP, hold live
WebSockets, then the host sends announcements. Measures join success,
delivery success, and wall-clock fan-out latency (send -> last device).
"""
import argparse
import asyncio
import json
import random
import statistics
import time

import httpx
import websockets

LANGS = ["es", "fr", "zh", "ar", "pt", "ru", "bn", "it", "en", "asl"]


async def attendee(base, ws_base, code, i, results, announce_total):
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{base}/v1/events/{code}/join",
                              headers={"x-forwarded-for": f"10.0.{i // 250}.{i % 250}"},
                              json={"kind": "person", "name": f"user{i}",
                                    "language": random.choice(LANGS),
                                    "modality": "captions"})
        if r.status_code != 200:
            results["join_fail"] += 1
            return
        endpoint = r.json()["endpoint_id"]
    results["joined"] += 1
    try:
        async with websockets.connect(f"{ws_base}/v1/deliveries/subscribe/{endpoint}",
                                      open_timeout=30, max_queue=None) as ws:
            results["connected"] += 1
            got = 0
            while got < announce_total:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
                if msg.get("delivered"):
                    results["deliveries"] += 1
                    results["latencies"].append(time.monotonic())
                got += 1
    except Exception as exc:
        results["ws_fail"] += 1


async def main(users: int, base: str, announcements: int):
    ws_base = base.replace("http", "ws", 1)
    async with httpx.AsyncClient(timeout=30) as client:
        ev = (await client.post(f"{base}/v1/events",
                                json={"name": f"Load Test {users}",
                                      "invite_code": "letmein"})).json()
        code = ev["access_code"]
        info = (await client.get(f"{base}/v1/events/{code}")).json()
        channel = info["channel_id"]

    results = {"joined": 0, "join_fail": 0, "connected": 0, "ws_fail": 0,
               "deliveries": 0, "latencies": []}
    tasks = [asyncio.create_task(
        attendee(base, ws_base, code, i, results, announcements))
        for i in range(users)]

    # let everyone join and connect
    for _ in range(120):
        await asyncio.sleep(0.5)
        if results["connected"] + results["ws_fail"] + results["join_fail"] >= users:
            break
    print(f"joined={results['joined']}/{users}  ws_connected={results['connected']}")

    fanout_times = []
    async with httpx.AsyncClient(timeout=120) as client:
        for n in range(announcements):
            results["latencies"] = []
            t0 = time.monotonic()
            r = await client.post(f"{base}/v1/channels/{channel}/events",
                                  json={"channel_id": channel,
                                        "priority_class": "announcement",
                                        "payload": f"Announcement number {n}: the "
                                                   f"main doors open in {n + 5} minutes"})
            api_ms = (time.monotonic() - t0) * 1000
            await asyncio.sleep(3)  # allow all sockets to drain
            if results["latencies"]:
                last = (max(results["latencies"]) - t0) * 1000
            else:
                last = float("nan")
            fanout_times.append(last)
            print(f"announcement {n}: api={api_ms:.0f}ms  plans={r.json()['plans']}  "
                  f"delivered_to_sockets={len(results['latencies'])}  "
                  f"send->last_device={last:.0f}ms")

    await asyncio.sleep(2)
    for t in tasks:
        t.cancel()
    total_expected = results["connected"] * announcements
    print("\n===== RESULT =====")
    print(f"users:              {users}")
    print(f"joined ok:          {results['joined']}  (failures: {results['join_fail']})")
    print(f"websockets held:    {results['connected']}  (failures: {results['ws_fail']})")
    print(f"deliveries:         {results['deliveries']} / {total_expected} expected "
          f"({100 * results['deliveries'] / max(total_expected, 1):.2f}%)")
    ok = [f for f in fanout_times if f == f]
    if ok:
        print(f"fan-out (send -> last device): median {statistics.median(ok):.0f}ms, "
              f"worst {max(ok):.0f}ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=500)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--announcements", type=int, default=5)
    args = ap.parse_args()
    asyncio.run(main(args.users, args.url, args.announcements))
