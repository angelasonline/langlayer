"""Render Plane + Assurance — executes DeliveryPlans (W7 failover),
emits signed DeliveryReceipts, evaluates SLA, computes metrics."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

from .models import (Artifact, ContentEvent, DeliveryPlan, DeliveryReceipt,
                     Modality, now_ms)
from .providers import CacheMiss, ProviderError, ProviderRegistry
from .store import Store

SIGNING_KEY = b"demo-signing-key-rotate-in-prod"  # KMS-held Ed25519 in production


def _sign(receipt: DeliveryReceipt) -> str:
    body = receipt.model_dump(exclude={"signature"})
    canonical = json.dumps(body, sort_keys=True, default=str).encode()
    return hmac.new(SIGNING_KEY, canonical, hashlib.sha256).hexdigest()


async def execute_plan(plan: DeliveryPlan, event: ContentEvent,
                       registry: ProviderRegistry, store: Store) -> DeliveryReceipt:
    """Walk the source chain (W7). Budget exhaustion is failure -> next source."""
    start = time.monotonic()
    artifact: Artifact | None = None
    causes: list[str] = []
    failovers = 0

    for step in plan.source_chain:
        provider = registry.get(step.provider)
        if provider is None:
            causes.append(f"{step.provider}: not registered")
            continue
        remaining_ms = plan.e2e_budget_ms - (time.monotonic() - start) * 1000
        # Each attempt may use the remaining budget; TTFO budget bounds the first try.
        # Floor of 4s per attempt when the class budget allows it: flagship
        # models routinely exceed 2x TTFO on non streaming APIs. Emergency
        # stays fast because remaining_ms caps the attempt.
        timeout_s = max(min(remaining_ms, max(plan.ttfo_budget_ms * 2, 4000)), 50) / 1000
        try:
            artifact = await asyncio.wait_for(provider.render(plan, event), timeout_s)
            provider.circuit.record_success()
            provider.latencies_ms.append(int((time.monotonic() - start) * 1000))
            break
        except (ProviderError, asyncio.TimeoutError) as exc:
            if not isinstance(exc, CacheMiss):
                provider.circuit.record_failure()
            cause = "budget exceeded" if isinstance(exc, asyncio.TimeoutError) else str(exc)
            causes.append(f"{step.provider}: {cause}")
            failovers += 1

    elapsed_ms = int((time.monotonic() - start) * 1000)
    receipt = DeliveryReceipt(
        plan_id=plan.id, event_id=event.id, profile_id=plan.profile_id,
        artifact_id=artifact.id if artifact else None,
        delivered=artifact is not None,
        source_used=artifact.provider if artifact else None,
        failovers=failovers, failover_causes=causes,
        ttfo_ms=elapsed_ms if artifact else None,   # sim: first output ≈ completion
        e2e_ms=elapsed_ms if artifact else None,
        quality=artifact.quality_estimate if artifact else None,
    )

    # D7 — SLA evaluation against the plan's budgets
    violations = []
    if not receipt.delivered:
        violations.append("delivery_failed")
    else:
        if receipt.e2e_ms > plan.e2e_budget_ms:
            violations.append(f"e2e {receipt.e2e_ms}ms > budget {plan.e2e_budget_ms}ms")
        if receipt.quality is not None and receipt.quality < 0.85:
            violations.append(f"quality {receipt.quality} < 0.85 floor")
    receipt.sla_violations = violations
    receipt.sla_met = not violations
    receipt.signature = _sign(receipt)

    if artifact:
        store.artifacts[artifact.id] = artifact
    store.receipts[receipt.id] = receipt
    store.log("render-plane", "delivery", receipt.id)
    return receipt


async def process_event(event: ContentEvent, store: Store,
                        registry: ProviderRegistry) -> list[DeliveryReceipt]:
    """Ingest -> route -> render once per (language, modality, chain) -> fan out.

    300 attendees in Spanish captions cost ONE model call, not 300. Each
    recipient still gets an individual signed receipt.
    """
    from .routing import route
    if hasattr(store, "save_content_event"):
        await asyncio.to_thread(store.save_content_event, event)
    else:
        store.events[event.id] = event
    health = {name: p.circuit.state for name, p in registry.providers.items()}
    plans = route(event, store, health)
    for p in plans:
        store.plans[p.id] = p
        store.log("routing-engine", "plan", p.id)

    groups: dict[tuple, list[DeliveryPlan]] = {}
    for p in plans:
        key = (p.language, p.modality, tuple(s.provider for s in p.source_chain))
        groups.setdefault(key, []).append(p)

    async def run_group(members: list[DeliveryPlan]) -> list[DeliveryReceipt]:
        lead = await execute_plan(members[0], event, registry, store)
        if not lead.delivered and members[0].modality == Modality.sign:
            # Sign chain exhausted: deliver source text as captions rather
            # than nothing, with an honest cause on the receipt.
            fb = Artifact(plan_id=members[0].id, modality=Modality.captions,
                          language=members[0].language,
                          content=f"[sign video unavailable; captions fallback] {event.payload or ''}".strip(),
                          provider="captions-fallback", quality_estimate=0.5)
            store.artifacts[fb.id] = fb
            lead.artifact_id = fb.id
            lead.delivered = True
            lead.source_used = "captions-fallback"
            lead.failover_causes.insert(0, "sign video: no capable source; delivered as captions fallback")
            lead.signature = _sign(lead)
        out = [lead]
        lead_artifact = store.artifacts.get(lead.artifact_id) if lead.artifact_id else None
        for p in members[1:]:
            if lead_artifact is not None:
                art = Artifact(plan_id=p.id, modality=lead_artifact.modality,
                               language=lead_artifact.language,
                               content=lead_artifact.content,
                               provider=lead_artifact.provider,
                               quality_estimate=lead_artifact.quality_estimate)
                store.artifacts[art.id] = art
                r = DeliveryReceipt(
                    plan_id=p.id, event_id=event.id, profile_id=p.profile_id,
                    artifact_id=art.id, delivered=True,
                    source_used=lead.source_used, failovers=lead.failovers,
                    failover_causes=list(lead.failover_causes),
                    ttfo_ms=lead.ttfo_ms, e2e_ms=lead.e2e_ms,
                    quality=lead.quality)
            else:
                r = DeliveryReceipt(
                    plan_id=p.id, event_id=event.id, profile_id=p.profile_id,
                    artifact_id=None, delivered=False, source_used=None,
                    failovers=lead.failovers,
                    failover_causes=list(lead.failover_causes),
                    ttfo_ms=None, e2e_ms=None, quality=None)
            r.sla_violations = list(lead.sla_violations)
            r.sla_met = lead.sla_met
            r.signature = _sign(r)
            store.receipts[r.id] = r
            out.append(r)
        return out

    nested = await asyncio.gather(*(run_group(m) for m in groups.values()))
    receipts = [r for grp in nested for r in grp]
    if hasattr(store, "save_delivery_batch"):
        await asyncio.to_thread(store.save_delivery_batch, plans, receipts)

    # Production QE: judge after delivery (adds zero attendee latency),
    # update + re-sign receipts with the measured score.
    from .quality import judge, live_judging_available
    if live_judging_available() and event.payload:
        async def _score():
            done: dict[tuple, float] = {}
            for grp in groups.values():
                lead = grp[0]
                art = None
                lead_receipt = next((x for x in receipts if x.plan_id == lead.id), None)
                if lead_receipt and lead_receipt.artifact_id:
                    art = store.artifacts.get(lead_receipt.artifact_id)
                if art is None or art.content.startswith("["):
                    continue
                score = await judge(event.payload, art.content, lead.language)
                if score is None:
                    continue
                for p in grp:
                    rec = next((x for x in receipts if x.plan_id == p.id), None)
                    if rec and rec.delivered:
                        rec.quality = score
                        if score < 0.85 and "quality below floor" not in rec.sla_violations:
                            rec.sla_violations.append("quality below floor")
                            rec.sla_met = False
                        rec.signature = _sign(rec)
            if hasattr(store, "save_delivery_batch"):
                await asyncio.to_thread(store.save_delivery_batch, [], receipts)
        asyncio.get_event_loop().create_task(_score())
    return receipts


def _pct(sorted_vals: list[int], q: float) -> int | None:
    if not sorted_vals:
        return None
    idx = min(int(len(sorted_vals) * q), len(sorted_vals) - 1)
    return sorted_vals[idx]


def metrics(store: Store) -> dict:
    """Spec §2.5 — everything derives from receipts, the single source of truth."""
    rs = list(store.receipts.values())
    delivered = [r for r in rs if r.delivered]
    lat = sorted(r.e2e_ms for r in delivered if r.e2e_ms is not None)
    return {
        "deliveries": len(rs),
        "delivery_success_rate": round(len(delivered) / len(rs), 4) if rs else None,
        "e2e_ms": {"p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95), "p99": _pct(lat, 0.99)},
        "failover_activation_rate":
            round(sum(1 for r in rs if r.failovers) / len(rs), 4) if rs else None,
        "sla_met_rate": round(sum(1 for r in rs if r.sla_met) / len(rs), 4) if rs else None,
        "unmet_preference_rate":
            round(sum(1 for r in delivered if r.source_used == "pa-passthrough")
                  / len(delivered), 4) if delivered else None,
        "quality_mean": round(sum(r.quality for r in delivered if r.quality) /
                              len(delivered), 3) if delivered else None,
    }
