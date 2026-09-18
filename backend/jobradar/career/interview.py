"""Interview preparation for a specific posting.

Built around the gap between what the candidate's CV shows and what the posting asks for,
because that gap is what interviewers probe. Generic "tell me about yourself" advice is
freely available and worth nothing; what helps is knowing which of *your* claims will be
stress-tested, and having a concrete answer ready.

Every question is grounded in either the posting or the CV, and STAR answer skeletons are
built only from experience the CV actually states — with placeholders where the candidate
needs to supply a real number. Rehearsing a fabricated story is worse than not preparing.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..llm import available, complete_json
from ..models import Job
from .prompts import build as build_prompt
from .prompts import prompts as shared_prompts

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "role_read": {"type": "string"},
        "likely_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "why_asked": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["technical", "behavioural", "case", "domain", "screening"],
                    },
                    "answer_skeleton": {"type": "string"},
                },
                "required": ["question", "why_asked", "kind", "answer_skeleton"],
            },
        },
        "gaps_to_defend": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "gap": {"type": "string"},
                    "how_it_will_be_probed": {"type": "string"},
                    "how_to_handle": {"type": "string"},
                },
                "required": ["gap", "how_it_will_be_probed", "how_to_handle"],
            },
        },
        "technical_topics": {"type": "array", "items": {"type": "string"}},
        "questions_to_ask_them": {"type": "array", "items": {"type": "string"}},
        "company_research_prompts": {"type": "array", "items": {"type": "string"}},
        "first_90_days_pitch": {"type": "string"},
    },
    "required": ["role_read", "likely_questions", "gaps_to_defend", "technical_topics"],
}


async def prep(
    job: Job,
    *,
    cv_text: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Build an interview prep pack for one posting."""
    if not available():
        return {
            "available": False,
            "reason": (
                "Interview prep needs an LLM key (Gemini or Groq — both have free tiers). "
                "Add one to generate a pack grounded in this posting and your CV."
            ),
        }

    owns_client = client is None
    client = client or httpx.AsyncClient()
    try:
        payload = await complete_json(
            client,
            build_prompt(
                "interview",
                title=job.title,
                company=job.company,
                location=job.location or "",
                jd=(job.description or "(no description published — prepare from the title)")[
                    :5000
                ],
                cv=(cv_text[:10000] if cv_text else shared_prompts()["interview"]["no_cv"]),
            ),
            schema=SCHEMA,
        )
    finally:
        if owns_client:
            await client.aclose()

    if not isinstance(payload, dict):
        return {"available": False, "reason": "Prep generation failed. Try again."}

    payload["available"] = True
    return payload


def render(pack: dict[str, Any]) -> str:
    """Plain-text prep pack for the CLI."""
    if not pack.get("available"):
        return pack.get("reason", "Interview prep unavailable.")

    out: list[str] = []
    if pack.get("role_read"):
        out += ["WHAT THEY ACTUALLY NEED", f"  {pack['role_read']}", ""]

    if pack.get("likely_questions"):
        out.append("LIKELY QUESTIONS")
        for i, q in enumerate(pack["likely_questions"], 1):
            out.append(f"  {i}. [{q.get('kind', '')}] {q.get('question', '')}")
            out.append(f"     Testing: {q.get('why_asked', '')}")
            out.append(f"     Outline: {q.get('answer_skeleton', '')}")
            out.append("")

    if pack.get("gaps_to_defend"):
        out.append("GAPS THEY WILL PROBE")
        for g in pack["gaps_to_defend"]:
            out.append(f"  • {g.get('gap', '')}")
            out.append(f"    Probed as: {g.get('how_it_will_be_probed', '')}")
            out.append(f"    Handle it: {g.get('how_to_handle', '')}")
            out.append("")

    if pack.get("technical_topics"):
        out += ["REVISE", *[f"  • {t}" for t in pack["technical_topics"]], ""]

    if pack.get("questions_to_ask_them"):
        out += ["ASK THEM", *[f"  • {q}" for q in pack["questions_to_ask_them"]], ""]

    if pack.get("first_90_days_pitch"):
        out += ["FIRST 90 DAYS", f"  {pack['first_90_days_pitch']}", ""]

    return "\n".join(out)
