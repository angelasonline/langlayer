# Production setup (community-event scale, hundreds of attendees)

## 1. Database (makes spaces, attendees, receipts survive restarts)
Without DATABASE_URL the app uses a local SQLite file, which survives process
restarts but NOT redeploys (Render disks are ephemeral). For real durability:

Option A, free: create a Postgres database at neon.tech (free tier, does not
expire), copy its connection string.
Option B, one dashboard: Render > New > PostgreSQL (Basic, ~$7/mo), copy the
Internal Database URL.

Then: Render > your service > Environment > add
    DATABASE_URL = <the connection string>
Save. The app migrates itself on boot (creates its tables automatically).

## 2. Health checks
Render > Settings > Health Check Path: set to  /healthz
The endpoint also reports durability: {"durable": true, "db": "postgresql"}.

## 3. Instance and limits
- Pro ($25, 2GB/1CPU) comfortably serves a 500-1000 person space.
- IMPORTANT: run a SINGLE instance / single worker. Live WebSockets are held
  in process memory; multiple instances need the Redis pub/sub layer
  (stadium-scale roadmap). Do not enable autoscaling yet.

## 4. What the load test proved (repeat it yourself)
    uvicorn langlayer.api:app --port 8000
    python loadtest.py --users 500 --url http://localhost:8000
Sandbox result on 1 CPU: 500/500 joined, 500 sockets held, 2500/2500
deliveries (100%), median fan-out send-to-last-device 598ms.
Note: run with simulated providers this measures the delivery
infrastructure; with live AI providers, add one model call (~1-2s) per
language per announcement thanks to render-once fan-out, regardless of
attendee count.

## 5. Deploy-time behavior
Redeploys drop live sockets; attendee pages auto-reconnect within ~2s and
presence is revived from the database, so a deploy mid-event causes a blip,
not a loss. Prefer deploying between announcements anyway.

## 6. Monitoring (recommended next)
Add a free Sentry account (SENTRY_DSN) or at minimum watch Render's metrics
tab during your first real event. Alert on /healthz failures.

## 7. Backups and restore (verify before the first real event)
Neon: automatic point-in-time restore is on by default (check Projects >
Backups; free tier keeps ~24h history). Render Postgres: daily backups on
Basic and up (database page > Backups tab).

VERIFY A RESTORE ONCE, before it matters:
1. Create a throwaway space, join it, send one announcement.
2. Neon: Branches > create branch from a timestamp 5 minutes ago; point a
   local run at the branch URL and confirm the space loads. Render: Backups >
   download, restore into a scratch database, same check.
3. Delete the scratch. Write down how long it took; that is your real
   recovery time.

## 8. Operational envelope (current, honest)
- Single instance, single worker: REQUIRED (in-process sockets + limiter).
- Proven: 500 concurrent attendees, 100% delivery, sub-second fan-out.
- Expected safe ceiling on Pro (2GB/1CPU): ~1,500-2,000 sockets. Beyond
  that: Redis pub/sub + multi-instance (stadium roadmap).
- Rate limits (per IP): 5 spaces/hour, 30 joins/min, 60 announcements/min,
  20 voice/min. Tune in langlayer/ratelimit.py.
