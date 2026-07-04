"""Render Plane + Assurance — executes DeliveryPlans (W7 failover),
emits signed DeliveryReceipts, evaluates SLA, computes metrics."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time

from .models import (Artifact, ContentEvent, DeliveryPlan, DeliveryReceipt,
                     now_ms)
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
        timeout_s = max(min(remaining_ms, plan.ttfo_budget_ms * 2), 50) / 1000
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
    """Ingest -> route -> render/deliver -> assure, fanned out concurrently."""
    from .routing import route
    store.events[event.id] = event
    health = {name: p.circuit.state for name, p in registry.providers.items()}
    plans = route(event, store, health)
    for p in plans:
        store.plans[p.id] = p
        store.log("routing-engine", "plan", p.id)
    receipts = await asyncio.gather(
        *(execute_plan(p, event, registry, store) for p in plans))
    return list(receipts)


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
