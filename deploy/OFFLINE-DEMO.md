# Offline demo: language access with the internet unplugged

This runbook demonstrates Language Layer delivering translated announcements
with no internet connection, using a local model served by
[Mesh LLM](https://github.com/Mesh-LLM/mesh-llm) on the venue's LAN.

The mental model: the local model is a fire extinguisher. You stage it while
things are fine (online), and it is simply there when things are not.

## Why this requires running the server locally

Language Layer's cloud deployment obviously cannot be reached when a venue's
internet is down. The offline scenario is the venue box: the Language Layer
server and the mesh run on a machine on the venue's local network, and
attendee phones reach it over venue Wi-Fi. Nothing in the engine changes;
only where it runs.

## Stage 1: while online

1. Install Mesh LLM on the host machine (see the mesh-llm README for your
   platform) and download a translation-capable local model onto it. Do this
   while you have internet: the model is the extinguisher being staged.
2. Clone this repo on the same machine and install:

       pip install -r requirements.txt

3. Confirm the mesh serves its OpenAI-compatible endpoint (default
   `http://localhost:9337/v1`).

## Stage 2: go offline

1. Start the mesh in LAN-only discovery mode so peers find each other with
   no internet (per the mesh-llm docs):

       --mesh-discovery-mode mdns

2. Physically disconnect the internet (unplug the WAN uplink; leave the
   local Wi-Fi/router powered so phones can reach the host).
3. Start Language Layer pointed at the mesh:

       export MESH_BASE_URL=http://localhost:9337/v1
       export MESH_MODEL=<your staged model name>
       uvicorn langlayer.api:app --host 0.0.0.0 --port 8000

   Do NOT set ANTHROPIC_API_KEY / OPENAI_API_KEY (or leave them set: the
   cloud tiers will fail fast and the chain will fall through — that is the
   point of the drill).

## Stage 3: the drill

1. From a phone on the venue Wi-Fi, open `http://<host-LAN-IP>:8000/host`,
   create a space, and join it from a second phone as Spanish.
2. Send the emergency evacuation template: it delivers instantly from the
   pre-translated cache. Deterministic templates outrank live AI by design;
   the mesh is not even consulted.
3. Send a free-text announcement: the cloud tiers fail (no internet), the
   chain falls to `mesh-local`, and the translation arrives from the local
   model. The receipt records `source: mesh-local`, the failovers that led
   there, and an honest lower quality prior.
4. Tap the message to view its delivery record: the whole story is in the
   receipt.

## Honest limits of this tier

Local open models are weaker than frontier cloud models, especially for
low-resource languages and ASL gloss. This tier is degraded-but-present:
better than silence, honestly labeled, never pretending parity. Per-language
quality testing of your staged model is real work; do it before relying on
this tier for any language you serve. The cloud quality judge is unreachable
offline, so mesh receipts carry the adapter's stated prior rather than a
measured score.

## What this proves

The provider abstraction means offline capability was an adapter, not a
rewrite: `providers_mesh.py` implements the same `render(plan, event)`
contract as the cloud adapters, the circuit breaker treats it identically,
and the receipt/SLA machinery never knew anything changed.
