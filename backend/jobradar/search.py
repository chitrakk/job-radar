"""Relevance scoring and filtering.

Used by the scheduled pipeline (to decide what enters the corpus) and by the live API (to
rank an on-demand search). The frontend mirrors the same weighting in TypeScript so that
cached and live results sort consistently — if you change weights here, change them there.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

from .geo import matches_location
from .models import Job, Query, RemoteKind

# Where a match is found matters more than how often. A keyword in the title is a strong
# signal; the same word buried in a benefits paragraph is nearly noise.
W_TITLE = 10.0
W_SKILLS = 4.0
W_TAGS = 2.5
W_COMPANY = 2.0
W_BODY = 1.0

_WORD = re.compile(r"[a-z0-9+#.]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _field_score(terms: list[str], text: str, weight: float) -> float:
    if not text or not terms:
        return 0.0
    tokens = set(tokenize(text))
    blob = text.lower()
    hits = 0.0
    for term in terms:
        if term in tokens:
            hits += 1.0
        elif len(term) > 3 and term in blob:
            # Partial credit for substring matches ("analyst" inside "analytics").
            hits += 0.5
    return weight * hits


def recency_boost(job: Job, half_life_days: float = 14.0) -> float:
    """Exponential decay. A three-week-old posting is often already filled, and surfacing
    fresh ones is most of the value of running this every four hours."""
    stamp = job.posted_at or job.first_seen_at
    age_days = max(0.0, (datetime.now(UTC) - stamp).total_seconds() / 86400)
    return math.exp(-age_days / half_life_days)


def score(job: Job, query: Query) -> float:
    terms = [t for kw in query.keywords for t in tokenize(kw)]
    if not terms:
        base = 1.0
    else:
        raw = (
            _field_score(terms, job.title, W_TITLE)
            + _field_score(terms, " ".join(job.skills), W_SKILLS)
            + _field_score(terms, " ".join(job.tags), W_TAGS)
            + _field_score(terms, job.company, W_COMPANY)
            + _field_score(terms, job.description[:4000], W_BODY)
        )
        # Normalise by term count so a two-word and a five-word query score comparably.
        base = raw / (len(terms) * W_TITLE)

    s = base * (0.55 + 0.45 * recency_boost(job))

    if query.location:
        if matches_location(
            job.location,
            query.location,
            is_remote=job.remote == RemoteKind.REMOTE,
            description=job.description,
        ):
            s *= 1.35
        elif job.remote == RemoteKind.REMOTE:
            s *= 1.0  # remote elsewhere is still plausible, just not boosted
        else:
            s *= 0.6

    if query.remote_only and job.remote != RemoteKind.REMOTE:
        s *= 0.2

    # A posting with a real description is more useful than a bare title from a list endpoint.
    if len(job.description) > 400:
        s *= 1.08
    return round(s, 5)


def matches(job: Job, query: Query, *, min_score: float = 0.0) -> bool:
    if query.max_age_days:
        stamp = job.posted_at or job.first_seen_at
        if (datetime.now(UTC) - stamp).days > query.max_age_days:
            return False
    if query.remote_only and job.remote != RemoteKind.REMOTE:
        return False
    if query.strict_location and query.location:
        if not matches_location(
            job.location,
            query.location,
            is_remote=job.remote == RemoteKind.REMOTE,
            description=job.description,
        ):
            return False
    return job.score >= min_score


def rank(jobs: list[Job], query: Query, *, min_score: float = 0.02) -> list[Job]:
    """Score, filter and sort. `min_score` drops postings that merely mention a keyword."""
    for job in jobs:
        job.score = score(job, query)
    kept = [j for j in jobs if matches(j, query, min_score=min_score)]
    kept.sort(key=lambda j: j.score, reverse=True)
    return kept[: query.limit] if query.limit else kept
