"""Production quality estimation: LLM-as-judge, reference-free.

A second model scores each delivered artifact for meaning preservation
(0.0-1.0). Runs AFTER delivery so it never adds latency to the attendee;
the receipt is updated and re-signed with the measured score.

Uses whichever live key exists (Anthropic preferred, OpenAI fallback).
With no live keys (simulated providers), receipts keep the provider prior.
"""
from __future__ import annotations

import os
import re

import httpx

from .models import LANGUAGE_NAMES

JUDGE_PROMPT = (
    "You are a translation quality judge. Score how well OUTPUT preserves the "
    "meaning of SOURCE for a reader of {lang}. Consider accuracy, completeness, "
    "and that nothing was added. Respond with ONLY a number between 0.00 and "
    "1.00.\nSOURCE: {source}\nOUTPUT: {output}"
)


def _parse(text: str) -> float | None:
    m = re.search(r"(0?\.\d+|1\.0+|[01])", text)
    if not m:
        return None
    val = float(m.group(1))
    return max(0.0, min(1.0, val))


async def judge(source: str, output: str, language: str) -> float | None:
    prompt = JUDGE_PROMPT.format(lang=LANGUAGE_NAMES.get(language, language),
                                 source=source, output=output)
    ant = os.environ.get("ANTHROPIC_API_KEY")
    oai = os.environ.get("OPENAI_API_KEY")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            if ant:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": ant, "anthropic-version": "2023-06-01"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 8,
                          "messages": [{"role": "user", "content": prompt}]})
                if r.status_code == 200:
                    blocks = r.json().get("content", [])
                    return _parse("".join(b.get("text", "") for b in blocks))
            if oai:
                r = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {oai}"},
                    json={"model": "gpt-4o-mini", "max_tokens": 8,
                          "messages": [{"role": "user", "content": prompt}]})
                if r.status_code == 200:
                    return _parse(r.json()["choices"][0]["message"]["content"])
    except httpx.HTTPError:
        return None
    return None


def live_judging_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
