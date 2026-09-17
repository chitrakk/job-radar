"""Location matching and cross-source deduplication.

Both are where the corpus quality actually comes from: without geo the India corpus fills
with San Francisco roles, and without dedupe the same job appears four times.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobradar.dedupe import dedupe, normalise_company, normalise_title
from jobradar.geo import canonical_city, is_anywhere_remote, is_india, matches_location
from jobradar.models import Job, RemoteKind

# --------------------------------------------------------------------------- geo


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Bengaluru, Karnataka, India", "bengaluru"),
        ("Bangalore", "bengaluru"),
        ("Gurgaon", "gurugram"),
        ("Bombay", "mumbai"),
        ("Trivandrum", "thiruvananthapuram"),
        ("San Francisco, CA", None),
    ],
)
def test_canonical_city_resolves_aliases(location: str, expected: str | None) -> None:
    assert canonical_city(location) == expected


def test_indiana_is_not_india() -> None:
    """ "IN" as a US state code must not read as India, or every Indiana job leaks in."""
    assert is_india("Bloomington, IN") is False
    assert is_india("Indianapolis, Indiana") is False
    assert is_india("Bengaluru, India") is True


@pytest.mark.parametrize(
    ("job_location", "query", "is_remote", "expected"),
    [
        ("Bengaluru, Karnataka, India", "India", False, True),
        ("Gurgaon", "India", False, True),
        ("San Francisco, CA", "India", False, False),
        # A worldwide-remote role is a good outcome for an India-based candidate.
        ("Anywhere in the World", "India", True, True),
        ("Remote", "India", True, True),
        # ...but a region-locked one is not.
        ("Remote - USA", "India", True, False),
        ("Remote (US only)", "India", True, False),
        # City queries
        ("Bengaluru", "Bengaluru", False, True),
        ("Hyderabad", "Bengaluru", False, False),
        ("India", "Bengaluru", False, True),  # country-level posting may serve the city
    ],
)
def test_matches_location(job_location: str, query: str, is_remote: bool, expected: bool) -> None:
    assert matches_location(job_location, query, is_remote=is_remote) is expected


def test_global_in_description_does_not_make_a_job_worldwide() -> None:
    """Regression: matching "global"/"worldwide" in the description let San Francisco and
    Foster City roles through an India-only filter, because almost every job description
    contains boilerplate like "our global team"."""
    assert (
        is_anywhere_remote("San Francisco, CA", "Join our global team serving customers worldwide")
        is False
    )
    assert (
        matches_location(
            "San Francisco, CA", "India", is_remote=True, description="our global team"
        )
        is False
    )


def test_explicit_us_only_in_description_blocks_a_bare_remote() -> None:
    assert is_anywhere_remote("Remote", "You must be located in the US.") is False


# --------------------------------------------------------------------------- dedupe


def _job(title: str, company: str, location: str = "Bengaluru", **kw) -> Job:
    return Job(
        title=title, company=company, location=location, url=kw.pop("url", "https://x/1"), **kw
    )


def test_normalisers_strip_noise() -> None:
    assert normalise_company("Acme Technologies Pvt. Ltd.") == normalise_company("Acme")
    assert normalise_title("Sr. Data Analyst") == normalise_title("Senior Data Analyst")
    assert normalise_title("Data Analyst (Remote)") == normalise_title("Data Analyst")


def test_dedupe_collapses_the_same_role_across_sources() -> None:
    jobs = [
        _job(
            "Senior Data Analyst",
            "Acme Technologies Pvt Ltd",
            url="https://linkedin/1",
            source="linkedin",
        ),
        _job(
            "Sr. Data Analyst",
            "Acme",
            url="https://boards.greenhouse.io/acme/1",
            source="ats:greenhouse",
        ),
    ]
    out = dedupe(jobs)
    assert len(out) == 1


def test_dedupe_keeps_genuinely_different_roles() -> None:
    jobs = [
        _job("Data Analyst", "Acme"),
        _job("Product Manager", "Acme"),
        _job("Data Analyst", "Globex"),
    ]
    assert len(dedupe(jobs)) == 3


def test_dedupe_prefers_the_ats_record_and_merges_complementary_fields() -> None:
    """Duplicates are complementary — the ATS has the description, LinkedIn has the date,
    an aggregator has the salary. Merging beats picking one."""
    older = datetime.now(UTC) - timedelta(days=5)
    jobs = [
        _job(
            "Data Analyst",
            "Acme",
            url="https://linkedin/1",
            source="linkedin",
            posted_at=older,
        ),
        _job(
            "Data Analyst",
            "Acme",
            url="https://boards.greenhouse.io/acme/1",
            source="ats:greenhouse",
            description="A long and useful job description." * 5,
            salary_min=1_200_000,
            salary_max=1_800_000,
            salary_currency="INR",
        ),
    ]
    out = dedupe(jobs)
    assert len(out) == 1
    merged = out[0]
    assert merged.source == "ats:greenhouse"  # the authoritative record survives
    assert merged.salary_min == 1_200_000
    assert merged.description
    assert merged.posted_at == older  # earliest sighting wins
    assert "also:linkedin" in merged.tags


def test_dedupe_does_not_merge_the_same_title_in_different_cities() -> None:
    jobs = [_job("Data Analyst", "Acme", "Bengaluru"), _job("Data Analyst", "Acme", "Mumbai")]
    # Identity includes location, so these stay distinct roles.
    assert len({j.identity() for j in jobs}) == 2


def test_remote_kind_is_preserved_through_merge() -> None:
    jobs = [
        _job("Data Analyst", "Acme", source="linkedin"),
        _job("Data Analyst", "Acme", source="ats:lever", remote=RemoteKind.REMOTE),
    ]
    assert dedupe(jobs)[0].remote == RemoteKind.REMOTE
