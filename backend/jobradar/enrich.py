"""LLM enrichment of newly-seen postings.

Runs only on jobs the corpus has not seen before, and the result is cached by content hash
in store.py, so a posting is enriched once in its lifetime no matter how many times it is
re-scraped or how many people visit the site. That is what keeps a free-tier key viable:
cost tracks new postings per day, not traffic.

Extracts the things sources do not reliably publish — skills, a real seniority level, a
one-line summary, and a usable location for freeform sources like HN Who-is-Hiring.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .llm import available, complete_json
from .models import Job, Seniority
from .taxonomy import extract_skills
from .textutil import detect_seniority

log = logging.getLogger(__name__)

# Jobs per request. Batching is what makes the free tier workable — one call per job would
# blow through a 10 RPM limit immediately. Eight keeps the prompt well inside Gemini's
# token budget while cutting call count ~8x.
BATCH_SIZE = 8

# How much of each description the model sees. Job pages carry long legal and benefits
# boilerplate that adds no signal and crowds out the parts that matter.
DESC_CHARS = 2500

SCHEMA = {
    "type": "object",
    "properties": {
        "jobs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "seniority": {
                        "type": "string",
                        "enum": ["intern", "entry", "mid", "senior", "lead", "exec", "unknown"],
                    },
                    "summary": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "location": {"type": "string"},
                },
                "required": ["i", "skills", "seniority", "summary"],
            },
        }
    },
    "required": ["jobs"],
}

PROMPT = """You are indexing job postings for a job search tool used by candidates in India.

For each posting below, extract:
- skills: 4-8 concrete, searchable technical or domain skills actually required. Use
  canonical names ("PostgreSQL" not "postgres db", "Power BI" not "powerbi"). Never invent
  a skill the posting does not ask for.
- seniority: one of intern, entry, mid, senior, lead, exec, unknown. Judge from required
  years and scope, not just the job title.
- summary: ONE sentence, max 22 words, stating what the person will actually do. No
  marketing language, no "exciting opportunity", no restating the title.
- tags: 0-4 short lowercase category labels, e.g. "fintech", "saas", "startup", "analytics".
- location: the work location if you can tell from the text, else "". Some postings put it
  only in the body. Prefer a city name. Use "Remote" only if genuinely location-free.

Return JSON: {{"jobs": [{{"i": <index>, "skills": [...], "seniority": "...",
"summary": "...", "tags": [...], "location": "..."}}]}}

Include every index exactly once. Postings:

{postings}"""


def _heuristic(job: Job) -> None:
    """Fallback when no LLM is configured or both providers fail.

    Worse than the model, but it keeps the fields search actually depends on populated.
    Without this, a corpus built with no API key has empty `skills` on every job, and
    since search matches title/company/skills/tags/summary, three of those five fields
    being blank made search effectively title-only.
    """
    job.seniority = detect_seniority(job.title, job.description)
    if not job.skills:
        job.skills = extract_skills(job.title, job.description)


def _render(batch: list[Job]) -> str:
    lines = []
    for i, job in enumerate(batch):
        desc = job.description[:DESC_CHARS] if job.description else "(no description published)"
        lines.append(
            f"--- {i} ---\nTitle: {job.title}\nCompany: {job.company}\n"
            f"Location field: {job.location or '(empty)'}\nDescription: {desc}"
        )
    return "\n\n".join(lines)


def _apply(batch: list[Job], payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    rows = payload.get("jobs")
    if not isinstance(rows, list):
        return 0

    applied = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        idx = row.get("i")
        if not isinstance(idx, int) or not 0 <= idx < len(batch):
            continue
        job = batch[idx]

        skills = [str(s).strip() for s in (row.get("skills") or []) if str(s).strip()]
        # Fall back to vocabulary extraction if the model returned none.
        job.skills = skills[:8] or extract_skills(job.title, job.description)

        level = str(row.get("seniority", "")).lower()
        try:
            job.seniority = Seniority(level)
        except ValueError:
            job.seniority = detect_seniority(job.title, job.description)

        summary = str(row.get("summary", "")).strip()
        # Guard against the model returning a paragraph despite the instruction.
        job.summary = summary[:220]

        tags = [str(t).strip().lower() for t in (row.get("tags") or []) if str(t).strip()]
        # Keep dedupe's cross-posting markers, which live in the same field.
        preserved = [t for t in job.tags if t.startswith("also:")]
        job.tags = sorted(set(tags[:4]) | set(preserved))[:16]

        # Only fill a location we do not already have; a source's own field is more
        # trustworthy than a model reading it out of prose.
        inferred = str(row.get("location", "")).strip()
        if inferred and not job.location:
            job.location = inferred[:120]

        job.enriched = True
        applied += 1
    return applied


async def enrich_new(jobs: list[Job], *, concurrency: int = 3) -> int:
    """Enrich `jobs` in place. Returns how many were successfully enriched."""
    if not jobs:
        return 0

    if not available():
        log.info("no LLM key configured; using heuristics for %d jobs", len(jobs))
        for job in jobs:
            _heuristic(job)
        return 0

    batches = [jobs[i : i + BATCH_SIZE] for i in range(0, len(jobs), BATCH_SIZE)]
    log.info("enriching %d new jobs in %d batches", len(jobs), len(batches))

    sem = asyncio.Semaphore(concurrency)
    enriched = 0

    async with httpx.AsyncClient() as client:

        async def one(batch: list[Job]) -> int:
            async with sem:
                payload = await complete_json(
                    client, PROMPT.format(postings=_render(batch)), schema=SCHEMA
                )
            if payload is None:
                for job in batch:
                    _heuristic(job)
                return 0
            n = _apply(batch, payload)
            # Any job the model skipped still needs a seniority for the UI filter.
            for job in batch:
                if not job.enriched:
                    _heuristic(job)
            return n

        results = await asyncio.gather(*(one(b) for b in batches), return_exceptions=True)

    for r in results:
        if isinstance(r, int):
            enriched += r
        else:
            log.warning("enrichment batch failed: %s", r)

    log.info("enriched %d/%d jobs", enriched, len(jobs))
    return enriched


def enrich_cache_key(job: Job) -> str:
    """Stable key for the enrichment cache: identity plus a description fingerprint, so an
    edited posting gets re-enriched but an unchanged one never does."""
    return f"{job.id}:{len(job.description)}"


__all__ = ["enrich_cache_key", "enrich_new"]
