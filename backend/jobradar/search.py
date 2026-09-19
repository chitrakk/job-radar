"""Relevance scoring and filtering.

Used by the scheduled pipeline (to decide what enters the corpus) and by the live API. The
frontend mirrors the same model in TypeScript so cached and live results sort consistently
— if you change the tiers or weights here, change them in frontend/src/lib/search.ts, and
tests/test_relevance.py will tell you if the two drift.

The scoring model is tiered rather than additive. An additive bag-of-words score cannot
separate "Data Analyst" from "Data Engineer" when the query is "data analyst", because both
contain a strong query term and the difference is *which* terms and *where*. Measured on
the live corpus, that returned 115 results for "data analyst" of which 17 were relevant.

Tiers, highest first:
  1. exact query phrase in the title              ("Senior Data Analyst")
  2. a known alias of the same role in the title  ("BI Analyst", "Business Analyst")
  3. every query term present in the title        ("Analyst, Data Platform")
  4. some query terms in the title                (weak)
  5. body / skills evidence only                  (weakest)

A title that matches a *sibling* role's canonical name is then demoted, so a Data Engineer
does not outrank a Data Analyst just because it contains "data".
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime

from .geo import locality, matches_location
from .models import Job, Query, RemoteKind
from .taxonomy import Intent, is_noise_title, normalise, seniority_distance, understand

# Tier scores. The gaps are deliberately wide: a title match should never be overtaken by
# an accumulation of body matches, which is exactly how bag-of-words ranking goes wrong.
T_EXACT_PHRASE = 1.00
T_ALIAS = 0.82
T_ALL_TERMS = 0.70
# Every query term is in the job's own text, just not in its title. Without a tier here a
# one-word skill search could not match anything at all: "tableau" resolves to no role
# family, so there are no skills to corroborate with, and a single body hit scored exactly
# W_BODY_TERM — which the guard below then rejected as noise. On the live corpus
# "tableau", "statistics", "excel" and "pandas" each returned nothing and "sql" returned
# one result, in an index where those are the most frequently listed skills.
T_BODY_ALL_TERMS = 0.44
T_PARTIAL = 0.34
T_BODY_ONLY = 0.16

# Corroboration, added on top of the tier rather than replacing it.
W_SKILL_HIT = 0.030
W_BODY_TERM = 0.020
W_COMPANY = 0.015
MAX_CORROBORATION = 0.18

# How much of the title the query accounts for. "Data Analyst" is a cleaner match for
# "data analyst" than "Financial Data Analyst (SQL, Power BI-DAX)", which is a more
# specific job that happens to contain the phrase. Without this, the longer title wins on
# skill corroboration alone — it mentions SQL and Power BI, so it accumulates more
# evidence than the exact match it should be losing to.
W_TITLE_FOCUS = 0.12

# A title that names a sibling role keeps this fraction of its score.
RIVAL_PENALTY = 0.35
# Titles with no information ("See posting") are suppressed rather than dropped, so a
# corpus made only of them still returns something.
NOISE_PENALTY = 0.15

# Per rung of the seniority ladder between what the query asked for and what is offered.
# Without this, "entry level data scientist" and "principal data scientist" were the same
# search, both led by Sr and Principal roles.
SENIORITY_STEP_PENALTY = 0.20
MAX_SENIORITY_PENALTY = 0.62

# How much the *kind* of location match matters once a posting has passed the filter. A
# worldwide-remote role satisfies "Delhi", but somebody who typed Delhi wants Delhi.
LOCALITY_WEIGHT = {"exact": 1.25, "metro": 1.18, "region": 1.00, "remote": 0.78, "": 0.0}

_WORD = re.compile(r"[a-z0-9+#.]+")


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def recency_boost(job: Job, half_life_days: float = 14.0) -> float:
    """Exponential decay. A three-week-old posting is often already filled, and surfacing
    fresh ones is most of the value of running this every four hours."""
    stamp = job.posted_at or job.first_seen_at
    age_days = max(0.0, (datetime.now(UTC) - stamp).total_seconds() / 86400)
    return math.exp(-age_days / half_life_days)


def _title_tier(title_norm: str, intent: Intent) -> float:
    """Which tier the title earns."""
    if not intent.terms:
        return T_ALL_TERMS  # no query: everything is equally relevant

    if intent.normalised and intent.normalised in title_norm:
        return T_EXACT_PHRASE

    for alias in intent.aliases:
        if alias and alias in title_norm:
            return T_ALIAS

    title_tokens = set(title_norm.split())
    present = sum(1 for t in intent.terms if t in title_tokens)
    if present == len(intent.terms):
        return T_ALL_TERMS
    if present:
        # Partial credit scaled by how much of the query the title actually covers.
        return T_PARTIAL * (present / len(intent.terms))
    return 0.0


def _title_focus(title_norm: str, intent: Intent) -> float:
    """What fraction of the title the query accounts for, in 0..1.

    Uses the longest matching phrase, so an alias match ("BI Analyst" for "data analyst")
    is measured against the alias that actually matched rather than the raw query.
    """
    if not title_norm:
        return 0.0
    best = len(intent.normalised) if intent.normalised in title_norm else 0
    for alias in intent.aliases:
        if alias and alias in title_norm:
            best = max(best, len(alias))
    if not best:
        # No phrase matched; fall back to the share of title words the query covers.
        title_words = title_norm.split()
        if not title_words:
            return 0.0
        covered = sum(1 for w in title_words if w in intent.terms)
        return covered / len(title_words)
    return min(1.0, best / len(title_norm))


def _corroboration(job: Job, intent: Intent, haystack: str) -> float:
    """Evidence from skills, body text and company name."""
    score = 0.0
    for skill in intent.skills:
        if skill and skill in haystack:
            score += W_SKILL_HIT
    hay_tokens = set(haystack.split())
    for term in intent.terms:
        if term in hay_tokens:
            score += W_BODY_TERM
    if intent.terms and any(t in normalise(job.company) for t in intent.terms):
        score += W_COMPANY
    return min(score, MAX_CORROBORATION)


def _is_rival(title_norm: str, intent: Intent) -> bool:
    """Does the title name a different role that merely shares vocabulary?"""
    return any(rival and rival in title_norm for rival in intent.rivals)


def _seniority_fit(job: Job, intent: Intent) -> float:
    """Multiplier for offering a different experience level than the query asked for."""
    if not intent.seniority:
        return 1.0
    distance = seniority_distance(intent.seniority, str(job.seniority))
    if not distance:
        return 1.0
    return 1.0 - min(MAX_SENIORITY_PENALTY, distance * SENIORITY_STEP_PENALTY)


def score(job: Job, query: Query, intent: Intent | None = None) -> float:
    """Relevance of one job to one query, in roughly 0..1.3."""
    intent = intent or understand(query.as_text())

    title_norm = normalise(job.title)
    # Everything the job says about itself, for corroboration only.
    haystack = normalise(
        f"{job.title} {' '.join(job.skills)} {' '.join(job.tags)} "
        f"{job.summary} {job.description[:3000]}"
    )

    tier = _title_tier(title_norm, intent)
    corroboration = _corroboration(job, intent, haystack)

    if tier == 0.0:
        # Nothing in the title. Every term appearing in the job's own text is still a real
        # match — that is how a skill search like "tableau" finds anything at all.
        hay_tokens = set(haystack.split())
        if intent.terms and all(t in hay_tokens for t in intent.terms):
            base = T_BODY_ALL_TERMS + corroboration
        elif corroboration > W_BODY_TERM:
            base = T_BODY_ONLY + corroboration
        else:
            return 0.0
    else:
        base = tier + corroboration + W_TITLE_FOCUS * _title_focus(title_norm, intent)

    # A sibling role is a different job, however many query words it shares.
    if intent.has_family and _is_rival(title_norm, intent):
        # Unless it also matches us by name, e.g. "Data Analyst / Data Engineer".
        if not any(a and a in title_norm for a in intent.aliases):
            base *= RIVAL_PENALTY

    if is_noise_title(job.title):
        base *= NOISE_PENALTY

    base *= _seniority_fit(job, intent)

    # Freshness matters but must not reorder relevance tiers, so it is a gentle multiplier.
    s = base * (0.70 + 0.30 * recency_boost(job))

    if query.location:
        where = locality(
            job.location,
            query.location,
            is_remote=job.remote == RemoteKind.REMOTE,
            description=job.description,
        )
        # A job in the city beats one in the metro beats one merely in the country beats a
        # worldwide-remote role that matches whatever anybody types.
        s *= LOCALITY_WEIGHT[where] if where else 0.55

    if query.remote_only and job.remote != RemoteKind.REMOTE:
        s *= 0.2

    # A posting with a real description is more useful than a bare title from a list
    # endpoint, but this must stay small enough not to outrank a tier.
    if len(job.description) > 400:
        s *= 1.04
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
    intent = understand(query.as_text())
    for job in jobs:
        job.score = score(job, query, intent)
    kept = [j for j in jobs if matches(j, query, min_score=min_score)]
    # Ties broken by recency, so equally relevant jobs surface newest-first.
    kept.sort(key=lambda j: (j.score, (j.posted_at or j.first_seen_at).timestamp()), reverse=True)
    return kept[: query.limit] if query.limit else kept
