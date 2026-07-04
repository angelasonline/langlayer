"""Human interpreter dispatch: the production integration contract.

STATUS: architecture complete; live interpreter network integration pending.
The simulated tier (providers.HumanBridgeSim) fills the chain slot until a
vendor implements DispatchClient. Everything a VRI vendor or independent
interpreter network needs to integrate is defined here; the engine calls
only this interface and never learns vendor details.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass
class InterpreterRequest:
    """The layer -> vendor: a moment needs a human."""
    request_id: str
    space_name: str
    source_language: str
    target_language: str           # BCP-47; sign languages are language tags
    modality: str                  # speech | sign | captions
    priority_class: str            # conversational | live | emergency
    compliance_mode: str           # standard | hipaa | ferpa | gov
    context_summary: str           # never message content; venue context only
    max_wait_seconds: int


@dataclass
class InterpreterAssignment:
    """Vendor -> the layer: a human accepted."""
    request_id: str
    interpreter_id: str
    credentials: list[str]         # e.g. ["RID-certified", "CoreCHI"]
    eta_seconds: int
    session_join_url: str          # WebRTC/SIP endpoint the layer bridges into


@dataclass
class SessionReport:
    """Vendor -> the layer at completion: feeds the receipt + billing."""
    request_id: str
    started_at: str
    ended_at: str
    minutes_billed: float
    quality_flags: list[str] = field(default_factory=list)


class DispatchClient(Protocol):
    """Implemented per vendor. All methods are async."""

    async def request(self, req: InterpreterRequest) -> Optional[InterpreterAssignment]:
        """Return an assignment within req.max_wait_seconds, or None."""

    async def cancel(self, request_id: str) -> None: ...

    async def complete(self, request_id: str) -> SessionReport: ...


# Escalation triggers (engine-side policy, already wired):
#  - venue compliance_mode in {"hipaa", "gov"} and class == "conversational"
#  - attendee taps "request a human interpreter"
#  - measured quality score below venue threshold for 2 consecutive deliveries
