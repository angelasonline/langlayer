# Language Layer — proof report

Generated 2026-07-04T01:09:32+00:00 · provider mode: **simulated**

## Load run
60 events x 3 recipients = **180 deliveries** across
announcement / conversational / live / emergency classes, including a forced
primary-AI outage window covering ~20% of traffic.

## Results
| Check | Result |
|---|---|
| Delivery success rate | **1.0000** (target >= 0.999) |
| p50 / p95 / p99 end-to-end | 180 / 351 / 351 ms |
| SLA met rate | **1.0000** |
| Failover activation rate | 0.0833 (includes forced outage window) |
| Deliveries that survived the outage via failover | 3 — all delivered by fallback source |
| Emergency: 100% reach, cache source, within 2s budget | **PASS** (18 emergency deliveries) |
| Receipt signatures re-verified | **180/180** |
| Unmet preference rate | 0.0000 |
| Mean quality estimate | 0.952 |

## Test suite
`13 passed, 1 warning in 3.14s`

## Sample signed receipt (latest)
```json
{
  "id": "rcp_e8b567a2c88f4c43867f",
  "plan_id": "pln_b7c25f2f242643f88929",
  "event_id": "evt_01b567b6162a4301b314",
  "profile_id": "prf_ccf16a98d6af434385ee",
  "artifact_id": "art_bda9f34518344c629ef2",
  "delivered": true,
  "source_used": "cache",
  "failovers": 0,
  "failover_causes": [],
  "ttfo_ms": 0,
  "e2e_ms": 0,
  "quality": 0.99,
  "sla_tier": "gold",
  "sla_met": true,
  "sla_violations": [],
  "signature": "4be09ae83abe32af8fe950aba1ac0bd32bd78b838d70e587bc0036819248532d"
}
```

## Scope of this proof
This report demonstrates the *infrastructure* claims: routing correctness,
failover resilience, emergency determinism, receipt integrity, and SLA
accounting — with providers in **simulated** mode. Claims it does NOT
make: real-model translation quality and real-network latency. Re-run with
`OPENAI_API_KEY` set for live-model output, and use the 90-day pilot to
produce field numbers a customer or regulator can rely on.
