"""Data models — see spec §2.2. Trimmed to the fields the engine uses."""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def now_ms() -> int:
    return int(time.time() * 1000)


# Supported languages, alphabetical by English name.
# Fields: tag, name, rtl, voice (device speech voices typically available), tier
# tier: "strong" = consistently high model quality; "emerging" = usable, verify
# for high-stakes use; "sign" = text gloss only today (the largest gap in every
# frontier model: none can produce sign-language video).
LANGUAGES_FULL = [
    ("asl", "American Sign Language", False, False, "sign"),
    ("am", "Amharic", False, False, "emerging"),
    ("ar", "Arabic", True, True, "strong"),
    ("bn", "Bengali", False, False, "strong"),
    ("my", "Burmese", False, False, "emerging"),
    ("zh", "Chinese (Mandarin)", False, True, "strong"),
    ("cs", "Czech", False, True, "strong"),
    ("da", "Danish", False, True, "strong"),
    ("nl", "Dutch", False, True, "strong"),
    ("en", "English", False, True, "strong"),
    ("fa", "Farsi", True, False, "strong"),
    ("fi", "Finnish", False, True, "strong"),
    ("fr", "French", False, True, "strong"),
    ("de", "German", False, True, "strong"),
    ("el", "Greek", False, True, "strong"),
    ("ha", "Hausa", False, False, "emerging"),
    ("he", "Hebrew", True, True, "strong"),
    ("hi", "Hindi", False, True, "strong"),
    ("hu", "Hungarian", False, True, "strong"),
    ("id", "Indonesian", False, True, "strong"),
    ("it", "Italian", False, True, "strong"),
    ("ja", "Japanese", False, True, "strong"),
    ("km", "Khmer", False, False, "emerging"),
    ("ko", "Korean", False, True, "strong"),
    ("lo", "Lao", False, False, "emerging"),
    ("mn", "Mongolian", False, False, "emerging"),
    ("ne", "Nepali", False, False, "emerging"),
    ("nb", "Norwegian", False, True, "strong"),
    ("pl", "Polish", False, True, "strong"),
    ("pt", "Portuguese", False, True, "strong"),
    ("ro", "Romanian", False, True, "strong"),
    ("ru", "Russian", False, True, "strong"),
    ("si", "Sinhala", False, False, "emerging"),
    ("so", "Somali", False, False, "emerging"),
    ("es", "Spanish", False, True, "strong"),
    ("sw", "Swahili", False, False, "emerging"),
    ("tl", "Tagalog", False, False, "strong"),
    ("ta", "Tamil", False, False, "strong"),
    ("te", "Telugu", False, False, "strong"),
    ("th", "Thai", False, True, "strong"),
    ("tr", "Turkish", False, True, "strong"),
    ("uk", "Ukrainian", False, True, "strong"),
    ("ur", "Urdu", True, False, "strong"),
    ("vi", "Vietnamese", False, True, "strong"),
    ("yo", "Yoruba", False, False, "emerging"),
    ("zu", "Zulu", False, False, "emerging"),
]
LANGUAGES = [(t, n) for t, n, _r, _v, _tier in LANGUAGES_FULL]
LANGUAGE_NAMES = dict(LANGUAGES)
LANGUAGE_INFO = {t: {"tag": t, "name": n, "rtl": r, "voice": v, "tier": tier}
                 for t, n, r, v, tier in LANGUAGES_FULL}

MODALITY_LABELS = {
    "speech": "Spoken audio",
    "sign": "Sign language (text gloss for now)",
    "captions": "Captions",
    "translation": "Text",
    "audio_description": "Audio description",
    "simplified": "Plain language",
}


class Modality(str, Enum):
    speech = "speech"
    sign = "sign"
    captions = "captions"
    translation = "translation"
    audio_description = "audio_description"
    simplified = "simplified"


class PriorityClass(str, Enum):
    emergency = "emergency"
    conversational = "conversational"
    live = "live"
    announcement = "announcement"
    static = "static"


# Latency budgets (ms): time-to-first-output, end-to-end. Spec §2.1.
LATENCY_BUDGETS = {
    PriorityClass.emergency: (900, 2000),
    PriorityClass.conversational: (300, 8000),  # interim: flagship non streaming latency; restore 1000 with streaming
    PriorityClass.live: (1500, 8000),
    PriorityClass.announcement: (2000, 4000),
    PriorityClass.static: (300, 300),
}

# Default source chains per class. Emergency inverts: deterministic cache first.
DEFAULT_CHAINS = {
    PriorityClass.emergency: ["cache", "ai-realtime", "pa-passthrough"],
    PriorityClass.conversational: ["ai-realtime", "ai-realtime-alt", "human-bridge"],
    PriorityClass.live: ["ai-realtime", "cache", "human-bridge"],
    PriorityClass.announcement: ["cache", "ai-realtime", "ai-realtime-alt"],
    PriorityClass.static: ["cache", "ai-batch"],
}

# Which endpoint capabilities a modality needs.
MODALITY_NEEDS = {
    Modality.speech: {"audio_out"},
    Modality.sign: {"video_out"},
    Modality.captions: {"text_out"},
    Modality.translation: {"text_out"},
    Modality.audio_description: {"audio_out"},
    Modality.simplified: {"text_out"},
}


class LanguagePref(BaseModel):
    tag: str  # BCP-47; sign languages are first-class tags: "asl", "bfi", ...
    rank: int = 1


class ModalityPref(BaseModel):
    kind: Modality
    rank: int = 1


class ContextOverride(BaseModel):
    context: str  # "venue:<id>" | "channel:<id>" | "class:<priority>"
    languages: list[str] = []
    modalities: list[Modality] = []


class PreferenceSet(BaseModel):
    languages: list[LanguagePref] = []
    modalities: list[ModalityPref] = []
    overrides: list[ContextOverride] = []
    auto_detect: bool = False


class Profile(BaseModel):
    id: str = Field(default_factory=lambda: new_id("prf"))
    tenant_id: str = "tnt_demo"
    display_name: Optional[str] = None
    preferences: PreferenceSet = PreferenceSet()
    session_override: Optional[dict] = None  # live switch (W9)


class Endpoint(BaseModel):
    id: str = Field(default_factory=lambda: new_id("end"))
    profile_id: Optional[str] = None
    kind: str = "mobile"
    capabilities: set[str] = {"audio_out", "video_out", "text_out"}


class PresenceSession(BaseModel):
    id: str = Field(default_factory=lambda: new_id("prs"))
    profile_id: str
    endpoint_id: str
    attached_to: list[str] = []           # "venue:<id>" / "channel:<id>"
    attention: str = "active"             # active | passive
    ttl_seconds: int = 120
    last_heartbeat_ms: int = Field(default_factory=now_ms)

    @property
    def expired(self) -> bool:
        return now_ms() - self.last_heartbeat_ms > self.ttl_seconds * 1000


class Venue(BaseModel):
    id: str = Field(default_factory=lambda: new_id("vnu"))
    tenant_id: str = "tnt_demo"
    name: str
    compliance_mode: str = "standard"     # standard | hipaa | ferpa | gov
    sla_tier: str = "gold"


class Channel(BaseModel):
    id: str = Field(default_factory=lambda: new_id("chn"))
    venue_id: str
    name: str
    default_class: PriorityClass = PriorityClass.announcement


class ContentEvent(BaseModel):
    id: str = Field(default_factory=lambda: new_id("evt"))
    channel_id: str
    priority_class: PriorityClass
    kind: str = "text"                    # text | audio_segment | template_ref
    source_language: str = "en"
    payload: str = ""
    template: Optional[str] = None
    slots: dict = {}
    created_at_ms: int = Field(default_factory=now_ms)


class ChainStep(BaseModel):
    provider: str
    role: str = "fallback"


class DeliveryPlan(BaseModel):
    id: str = Field(default_factory=lambda: new_id("pln"))
    event_id: str
    profile_id: str
    language: str
    modality: Modality
    endpoint_id: str
    source_chain: list[ChainStep]
    ttfo_budget_ms: int
    e2e_budget_ms: int
    priority_class: PriorityClass
    decisions: dict[str, str] = {}        # D1–D6 reason strings (audit)


class Artifact(BaseModel):
    id: str = Field(default_factory=lambda: new_id("art"))
    plan_id: str
    modality: Modality
    language: str
    content: str
    provider: str
    quality_estimate: float


class DeliveryReceipt(BaseModel):
    id: str = Field(default_factory=lambda: new_id("rcp"))
    plan_id: str
    event_id: str
    profile_id: str
    artifact_id: Optional[str]
    delivered: bool
    source_used: Optional[str]
    failovers: int
    failover_causes: list[str] = []
    ttfo_ms: Optional[int]
    e2e_ms: Optional[int]
    quality: Optional[float]
    sla_tier: str = "gold"
    sla_met: bool = False
    sla_violations: list[str] = []
    signature: str = ""
