# Deploying the demo

Any of these gets you a public URL for the pitch. Set OPENAI_API_KEY in the
host's environment for live-model mode; omit it for the always-works simulated
mode. This build uses in-memory storage — perfect for a demo, reset on restart;
the Phase-1 hardening in the spec swaps in Postgres behind the same Store
interface before any pilot traffic.

## Option A — any box with Docker (5 minutes)
    docker build -t langlayer . && docker run -p 8000:8000 -e OPENAI_API_KEY=$OPENAI_API_KEY langlayer
    # or: cd deploy && docker compose up -d

## Option B — Fly.io (free tier, public HTTPS URL)
    fly launch --copy-config --config deploy/fly.toml
    fly secrets set OPENAI_API_KEY=sk-...   # optional
    fly deploy

## Option C — Render.com (no CLI)
Push the repo to GitHub, New > Blueprint, select the repo (deploy/render.yaml
is auto-detected), add OPENAI_API_KEY if desired, deploy.

## After deploy — the three URLs
    https://<your-app>/demo        operator console + three phones (the pitch)
    https://<your-app>/rider       open on an actual phone; pick a profile
    https://<your-app>/dashboard   live ops: metrics, provider health, receipts

Meeting checklist: rehearse in simulated mode (works with no network beyond the
page load), verify /dashboard shows green after a few sends, and keep a local
`uvicorn langlayer.api:app` as the offline backup.
