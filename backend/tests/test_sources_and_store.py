"""Source adapters against recorded payloads, plus corpus round-tripping.

These use canned responses rather than live network calls so CI is deterministic and does
not hammer the boards that are doing us a favour by publishing open endpoints.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from jobradar.models import Job, Query, SourceHealth, Tier
from jobradar.search import rank, score
from jobradar.sources.ats import PROVIDERS, GreenhouseSource, LeverSource
from jobradar.sources.base import Source, register, registry
from jobradar.store import Corpus

GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "title": "Senior Data Analyst",
            "location": {"name": "Bengaluru, India"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "content": "<p>Work with <b>SQL</b> and Python.</p><ul><li>5 years</li></ul>",
            "updated_at": "2026-09-01T10:00:00Z",
        }
    ]
}

LEVER_PAYLOAD = [
    {
        "text": "Product Manager",
        "categories": {"location": "Remote", "commitment": "Full-time"},
        "hostedUrl": "https://jobs.lever.co/acme/2",
        "applyUrl": "https://jobs.lever.co/acme/2/apply",
        "descriptionPlain": "Own the roadmap.",
        "createdAt": 1756684800000,
    }
]


def test_greenhouse_normalises_into_the_canonical_shape() -> None:
    jobs = PROVIDERS["greenhouse"]["parse"]("acme", "Acme", GREENHOUSE_PAYLOAD)
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Senior Data Analyst"
    assert job.company == "Acme"
    assert job.location == "Bengaluru, India"
    # HTML must be flattened, not stored raw — it reaches both the UI and the LLM.
    assert "<b>" not in job.description
    assert "SQL" in job.description


def test_lever_reads_epoch_milliseconds() -> None:
    jobs = PROVIDERS["lever"]["parse"]("acme", "Acme", LEVER_PAYLOAD)
    posted = jobs[0].posted_at
    assert posted is not None
    # 1756684800000ms == 2025-09-01. Seconds would have given 1970, the classic symptom.
    assert (posted.year, posted.month, posted.day) == (2025, 9, 1)
    assert jobs[0].apply_url.endswith("/apply")


@respx.mock
async def test_ats_source_survives_one_dead_board() -> None:
    """A company that changed ATS returns 404. That must not take down the other boards."""
    respx.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/acme/").mock(
        return_value=httpx.Response(200, json=GREENHOUSE_PAYLOAD)
    )
    respx.get(url__startswith="https://boards-api.greenhouse.io/v1/boards/dead/").mock(
        return_value=httpx.Response(404)
    )

    src = GreenhouseSource({"companies": [{"token": "acme", "name": "Acme"}, {"token": "dead"}]})
    async with httpx.AsyncClient() as client:
        jobs, health = await src.run(client, Query(keywords=["data"]))

    assert health.ok is True
    assert len(jobs) == 1
    assert jobs[0].source == "ats:greenhouse"


@respx.mock
async def test_blocked_source_degrades_instead_of_raising() -> None:
    """Naukri answers datacenter IPs with 406. The run must continue and say so."""
    respx.get(url__startswith="https://api.lever.co/").mock(return_value=httpx.Response(406))

    src = LeverSource({"companies": ["acme"]})
    async with httpx.AsyncClient() as client:
        jobs, health = await src.run(client, Query())

    assert jobs == []
    assert health.ok is False
    assert "blocked" in health.error


@respx.mock
async def test_a_raising_source_is_reported_not_propagated() -> None:
    class Exploding(Source):
        name = "test:exploding"

        async def search(self, client, query):  # noqa: ANN001
            raise RuntimeError("boom")

    src = Exploding()
    async with httpx.AsyncClient() as client:
        jobs, health = await src.run(client, Query())
    assert jobs == []
    assert health.ok is False
    assert "boom" in health.error


def test_registry_rejects_duplicate_names() -> None:
    existing = next(iter(registry()))

    with pytest.raises(ValueError, match="duplicate"):

        @register
        class Clash(Source):
            name = existing

            async def search(self, client, query):  # noqa: ANN001
                return []


# --------------------------------------------------------------------------- ranking


def test_title_matches_outrank_body_matches() -> None:
    q = Query(keywords=["data analyst"], location="")
    in_title = Job(title="Data Analyst", company="A", url="https://x/1")
    in_body = Job(
        title="Office Manager",
        company="B",
        url="https://x/2",
        description="You will work with our data analyst team.",
    )
    assert score(in_title, q) > score(in_body, q)


def test_fresh_postings_outrank_stale_ones() -> None:
    q = Query(keywords=["data analyst"], location="")
    fresh = Job(title="Data Analyst", company="A", url="https://x/1", posted_at=datetime.now(UTC))
    stale = Job(
        title="Data Analyst",
        company="B",
        url="https://x/2",
        posted_at=datetime.now(UTC) - timedelta(days=60),
    )
    assert score(fresh, q) > score(stale, q)


def test_strict_location_excludes_rather_than_demotes() -> None:
    q = Query(keywords=["analyst"], location="India", strict_location=True, max_age_days=0)
    jobs = [
        Job(title="Analyst", company="A", location="Bengaluru, India", url="https://x/1"),
        Job(title="Analyst", company="B", location="San Francisco, CA", url="https://x/2"),
    ]
    kept = rank(jobs, q, min_score=0.0)
    assert [j.company for j in kept] == ["A"]


# --------------------------------------------------------------------------- store


def test_corpus_round_trip_preserves_descriptions(tmp_path) -> None:
    corpus = Corpus(tmp_path)
    jobs = [
        Job(
            title="Data Analyst",
            company="Acme",
            location="Bengaluru",
            url="https://x/1",
            description="A description that lives in a shard.",
            source="ats:greenhouse",
        )
    ]
    corpus.save(jobs, [SourceHealth(source="ats:greenhouse", tier=Tier.TIER1, ok=True)])

    loaded = corpus.load()
    assert len(loaded) == 1
    restored = next(iter(loaded.values()))
    assert restored.description == "A description that lives in a shard."
    # index.json must stay light — descriptions belong in the shards only.
    index = json.loads((tmp_path / "index.json").read_text())
    assert "description" not in index[0]


def test_merge_preserves_first_seen_and_paid_for_enrichment(tmp_path) -> None:
    corpus = Corpus(tmp_path)
    first_seen = datetime.now(UTC) - timedelta(days=3)
    stored = Job(
        title="Data Analyst",
        company="Acme",
        location="Bengaluru",
        url="https://x/1",
        first_seen_at=first_seen,
        skills=["SQL"],
        summary="Enriched summary",
        enriched=True,
    )
    existing = {stored.id: stored}

    rescraped = Job(title="Data Analyst", company="Acme", location="Bengaluru", url="https://x/1")
    out = corpus.merge(existing, [rescraped], window_days=60)

    assert len(out) == 1
    # Re-scraping an unchanged posting must not reset its age or re-spend LLM quota.
    assert out[0].first_seen_at == first_seen
    assert out[0].enriched is True
    assert out[0].skills == ["SQL"]


def test_merge_drops_postings_past_the_window(tmp_path) -> None:
    corpus = Corpus(tmp_path)
    old = Job(
        title="Old Role",
        company="Acme",
        url="https://x/1",
        posted_at=datetime.now(UTC) - timedelta(days=200),
    )
    out = corpus.merge({old.id: old}, [], window_days=60)
    assert out == []


def test_save_removes_shards_that_no_longer_have_jobs(tmp_path) -> None:
    corpus = Corpus(tmp_path)
    job = Job(title="A", company="B", url="https://x/1", description="d")
    corpus.save([job], [])
    assert list((tmp_path / "jobs").glob("*.json"))

    corpus.save([], [])
    # Stale shards would otherwise serve descriptions for jobs that left the corpus.
    assert not list((tmp_path / "jobs").glob("*.json"))
