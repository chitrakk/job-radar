"""Canonical data shapes.

Every source adapter normalises into `Job`. Nothing downstream — dedupe, enrich,
ranking, storage, the frontend — knows or cares which site a posting came from.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

# Salary strings vary wildly across Indian boards: "12-18 LPA", "₹15,00,000 - ₹22,00,000 PA",
# "Rs 8 Lakh", "$120k". Parsing lives in salary.py; these are just the storage units.
CURRENCY_ALIASES = {
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
}


class Tier(StrEnum):
    """How much we trust a source to keep working.

    TIER1 sources are public, unauthenticated APIs that do not block datacenter IPs.
    TIER2 sources are scraped and are expected to fail intermittently from cloud runners;
    a TIER2 failure degrades a run, it never fails one.
    """

    TIER1 = "tier1"
    TIER2 = "tier2"


class Seniority(StrEnum):
    INTERN = "intern"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    EXEC = "exec"
    UNKNOWN = "unknown"


class RemoteKind(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


def _squash(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace, for identity comparisons."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


class Job(BaseModel):
    """One job posting, normalised.

    `id` is derived from company+title+location rather than the source URL, because the
    same role is routinely posted to Naukri, LinkedIn and the company's own ATS with three
    different URLs. Deriving identity from content is what lets dedupe.py collapse them.
    """

    id: str = ""
    title: str
    company: str
    location: str = ""
    country: str = ""
    remote: RemoteKind = RemoteKind.UNKNOWN

    description: str = ""
    url: str
    apply_url: str = ""

    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    salary_period: str = ""  # "year" | "month" | "hour"
    salary_text: str = ""  # the raw string, kept so the UI can show what the board said

    source: str = ""
    source_tier: Tier = Tier.TIER1
    posted_at: datetime | None = None
    first_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Populated by enrich.py in phase 2. Absent values must never break the frontend,
    # so every field has a usable zero value rather than being optional.
    skills: list[str] = Field(default_factory=list)
    seniority: Seniority = Seniority.UNKNOWN
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    enriched: bool = False

    # Set per-query at ranking time, not persisted as truth about the job itself.
    score: float = 0.0

    @field_validator("title", "company", "location", mode="before")
    @classmethod
    def _clean(cls, v: Any) -> str:
        if v is None:
            return ""
        return re.sub(r"\s+", " ", str(v)).strip()

    @field_validator("posted_at", "first_seen_at", "last_seen_at", mode="after")
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        # Sources return a mix of naive and aware datetimes; naive ones are always UTC
        # in practice. Normalising here keeps every downstream comparison safe.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = self.content_id()

    def identity(self) -> str:
        """The natural key used for cross-source dedupe."""
        # Location is deliberately coarse (first comma-segment) so that "Bengaluru, Karnataka,
        # India" and "Bengaluru" collapse together.
        loc = _squash(self.location.split(",")[0]) if self.location else ""
        return f"{_squash(self.company)}|{_squash(self.title)}|{loc}"

    def content_id(self) -> str:
        return hashlib.sha256(self.identity().encode()).hexdigest()[:16]

    def index_entry(self) -> dict[str, Any]:
        """The trimmed shape written to index.json.

        The frontend loads this for every job at once, so it must stay small — full
        descriptions are sharded into jobs/*.json and fetched only when a card is opened.
        """
        return {
            "id": self.id,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "remote": self.remote.value,
            "url": self.apply_url or self.url,
            "source": self.source,
            "tier": self.source_tier.value,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "first_seen_at": self.first_seen_at.isoformat(),
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "salary_currency": self.salary_currency,
            "salary_text": self.salary_text,
            "skills": self.skills,
            "seniority": self.seniority.value,
            "summary": self.summary,
            "tags": self.tags,
        }


class SourceHealth(BaseModel):
    """Per-source outcome of one pipeline run.

    Published alongside the corpus so the frontend can show honestly that, say, Naukri
    returned nothing this run — rather than silently serving a thinner list.
    """

    source: str
    tier: Tier
    ok: bool
    jobs_found: int = 0
    error: str = ""
    duration_ms: int = 0
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Query(BaseModel):
    """A saved search from config/queries.yml, also the live-API request shape."""

    keywords: list[str] = Field(default_factory=list)
    location: str = ""
    country: str = "in"
    remote_only: bool = False
    max_age_days: int = 45
    limit: int = 200
    # When True, a posting must actually satisfy `location` (or be worldwide-remote) to be
    # kept, rather than merely being ranked lower. Without this an India search fills up
    # with San Francisco roles that happened to match a keyword strongly.
    strict_location: bool = False

    def as_text(self) -> str:
        return " ".join(self.keywords)
