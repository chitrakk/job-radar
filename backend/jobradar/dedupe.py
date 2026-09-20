"""Cross-source deduplication.

The same role genuinely appears three or four times in one run: once on the company's
Greenhouse board, once via Adzuna (which indexed Naukri), once on LinkedIn. They carry
different URLs, differently-formatted company names ("Acme Technologies Pvt. Ltd." vs
"Acme"), and different location strings. Collapsing them is what separates a usable list
from a wall of repeats.

Strategy is two-pass: exact match on the normalised identity key, then fuzzy match within
a company block to catch title variations ("Sr. Data Analyst" / "Senior Data Analyst").
"""

from __future__ import annotations

import re
from collections import defaultdict

from rapidfuzz import fuzz

from .models import Job, RemoteKind, Tier

# Legal suffixes and boilerplate that differ per source but mean nothing for identity.
_COMPANY_NOISE = re.compile(
    r"\b(pvt\.?|private|ltd\.?|limited|llp|inc\.?|incorporated|corp\.?|corporation|"
    r"gmbh|plc|co\.?|company|technologies|technology|solutions|services|labs|india)\b",
    re.I,
)

# Title decorations that vary by board but describe the same role.
_TITLE_NOISE = re.compile(
    r"\b(urgent|immediate|hiring|opening|position|role|job|vacancy|"
    r"full[- ]time|part[- ]time|permanent|contract|wfh|remote|hybrid|onsite|"
    r"fresher|experienced|m/f/d|\(m/w/d\))\b",
    re.I,
)

_BRACKETS = re.compile(r"[\(\[\{].*?[\)\]\}]")

# Source preference when merging duplicates. Earlier wins: a company's own ATS posting is
# the most accurate and most likely to still be open.
_SOURCE_RANK = [
    "ats:greenhouse",
    "ats:lever",
    "ats:ashby",
    "ats:workable",
    "ats:smartrecruiters",
    "ats:recruitee",
    "adzuna",
    "linkedin",
    "remoteok",
    "remotive",
    "himalayas",
    "weworkremotely",
    "arbeitnow",
    "hn_hiring",
]


def normalise_company(name: str) -> str:
    n = _BRACKETS.sub(" ", name.lower())
    n = _COMPANY_NOISE.sub(" ", n)
    n = re.sub(r"[^a-z0-9]+", " ", n)
    return " ".join(n.split())


def normalise_title(title: str) -> str:
    t = _BRACKETS.sub(" ", title.lower())
    t = _TITLE_NOISE.sub(" ", t)
    t = re.sub(r"\bsr\.?\b", "senior", t)
    t = re.sub(r"\bjr\.?\b", "junior", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def _rank(job: Job) -> int:
    try:
        return _SOURCE_RANK.index(job.source)
    except ValueError:
        return len(_SOURCE_RANK)


def _merge_tags(primary: list[str], other: list[str]) -> list[str]:
    """Union two tag sets without letting them contradict each other.

    Tags of the form "Prefix: value" state a fact about the posting, and a plain union of
    two merged records produced cards carrying both "Experience: 3-8 yrs" and
    "Experience: 5-10 yrs" — 129 of them in one run. Two bands cannot both be right, and a
    card that states both is worse than one that states the surviving record's own.
    The primary is the higher-ranked source, so its value is the one to keep.
    """
    out: list[str] = []
    claimed: set[str] = set()
    for tag in list(primary) + list(other):
        if tag in out:
            continue
        prefix, sep, _ = tag.partition(":")
        if sep and not tag.startswith("also:"):
            key = prefix.strip().lower()
            if key in claimed:
                continue
            claimed.add(key)
        out.append(tag)
    return out[:16]


def _merge(primary: Job, other: Job) -> Job:
    """Fold `other` into `primary`, keeping the richest value for each field.

    Duplicates are complementary: LinkedIn has the posted date, the ATS has the full
    description, Adzuna has the salary. Merging beats picking one.
    """
    if len(other.description) > len(primary.description):
        primary.description = other.description
    if not primary.salary_min and other.salary_min:
        primary.salary_min = other.salary_min
        primary.salary_max = other.salary_max
        primary.salary_currency = other.salary_currency
        primary.salary_period = other.salary_period
        primary.salary_text = other.salary_text
    if not primary.posted_at and other.posted_at:
        primary.posted_at = other.posted_at
    elif primary.posted_at and other.posted_at:
        # Earliest sighting is the true posting date.
        primary.posted_at = min(primary.posted_at, other.posted_at)
    if primary.remote == RemoteKind.UNKNOWN and other.remote != RemoteKind.UNKNOWN:
        primary.remote = other.remote
    if not primary.location and other.location:
        primary.location = other.location
    if not primary.apply_url and other.apply_url:
        primary.apply_url = other.apply_url
    primary.tags = _merge_tags(primary.tags, other.tags)
    primary.first_seen_at = min(primary.first_seen_at, other.first_seen_at)
    primary.last_seen_at = max(primary.last_seen_at, other.last_seen_at)
    # Record every board this turned up on, so the UI can say "also on LinkedIn".
    extra = f"also:{other.source}"
    if other.source != primary.source and extra not in primary.tags:
        primary.tags.append(extra)
    return primary


def dedupe(jobs: list[Job], *, fuzzy_threshold: int = 88) -> list[Job]:
    """Collapse duplicate postings. Returns one Job per real-world role."""
    if not jobs:
        return []

    # Prefer higher-quality sources as the surviving record.
    ordered = sorted(jobs, key=lambda j: (_rank(j), -len(j.description)))

    # Pass 1: exact identity key.
    exact: dict[str, Job] = {}
    for job in ordered:
        key = f"{normalise_company(job.company)}|{normalise_title(job.title)}"
        if key in exact:
            _merge(exact[key], job)
        else:
            exact[key] = job

    # Pass 2: fuzzy title match within each company. Blocking by company keeps this O(n·k)
    # instead of O(n²) across the whole corpus.
    by_company: dict[str, list[Job]] = defaultdict(list)
    for job in exact.values():
        by_company[normalise_company(job.company)].append(job)

    out: list[Job] = []
    for company_jobs in by_company.values():
        kept: list[tuple[str, Job]] = []
        for job in company_jobs:
            norm = normalise_title(job.title)
            for kept_norm, kept_job in kept:
                if fuzz.token_sort_ratio(norm, kept_norm) >= fuzzy_threshold:
                    _merge(kept_job, job)
                    break
            else:
                kept.append((norm, job))
        out.extend(j for _, j in kept)

    # Recompute ids so they reflect the merged record.
    for job in out:
        job.id = job.content_id()
    return out


def tier_summary(jobs: list[Job]) -> dict[str, int]:
    counts = {"tier1": 0, "tier2": 0}
    for j in jobs:
        counts["tier1" if j.source_tier == Tier.TIER1 else "tier2"] += 1
    return counts
