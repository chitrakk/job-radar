"""Descriptions for sources whose list endpoint does not carry them.

LinkedIn's guest search returns titles only. That left 242 of the 557 jobs in the live
corpus with an empty description — and essentially every India-based one, since LinkedIn
is where the Indian postings come from. An empty description is not a cosmetic gap: it
silently disables skill extraction, skill search, interview prep and CV keyword alignment,
all of which read it. Measured before this existed, a bare "tableau" search returned zero
results and 79% of LinkedIn jobs had no skills at all.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from jobradar.models import Job
from jobradar.sources.linkedin_guest import LinkedInGuestSource

POSTING_HTML = """
<html><body>
  <div class="description__text">
    <section class="show-more-less-html">
      <div class="show-more-less-html__markup">
        <p>We are hiring a Data Scientist for our Gurugram team.</p>
        <ul>
          <li>Build forecasting models in Python and deploy them with Airflow</li>
          <li>Partner with analysts on experiment design and causal inference</li>
          <li>Strong SQL and Tableau required; 3-5 years of experience</li>
        </ul>
        <p>Compensation: 18-26 LPA depending on experience.</p>
      </div>
    </section>
  </div>
  <ul class="description__job-criteria-list">
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Employment type</h3>
      <span class="description__job-criteria-text">Full-time</span>
    </li>
    <li class="description__job-criteria-item">
      <h3 class="description__job-criteria-subheader">Job function</h3>
      <span class="description__job-criteria-text">Analyst</span>
    </li>
  </ul>
</body></html>
"""

JOB_URL = "https://in.linkedin.com/jobs/view/data-scientist-at-acme-4466190613"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4466190613"


def _job(url: str = JOB_URL) -> Job:
    return Job(title="Data Scientist", company="Acme", location="Gurugram, India", url=url)


@respx.mock
async def test_hydrate_fills_the_description() -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=POSTING_HTML))
    job = _job()
    async with httpx.AsyncClient() as client:
        assert await LinkedInGuestSource().hydrate(client, job) is True
    assert "forecasting models" in job.description
    assert "Airflow" in job.description


@respx.mock
async def test_hydrate_reads_salary_and_criteria() -> None:
    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=POSTING_HTML))
    job = _job()
    async with httpx.AsyncClient() as client:
        await LinkedInGuestSource().hydrate(client, job)
    # "18-26 LPA" is unambiguously rupees per year.
    assert job.salary_currency == "INR"
    assert job.salary_max == pytest.approx(2_600_000)
    assert "Full-time" in job.tags


@respx.mock
async def test_hydrated_text_makes_the_job_searchable_by_skill() -> None:
    """The point of the whole exercise: a posting nobody could find becomes findable."""
    from jobradar.models import Query
    from jobradar.search import rank
    from jobradar.taxonomy import extract_skills

    respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, text=POSTING_HTML))
    job = _job()
    query = Query(keywords=["tableau"], location="", max_age_days=0, limit=0)

    assert rank([job], query, min_score=0.02) == []

    async with httpx.AsyncClient() as client:
        await LinkedInGuestSource().hydrate(client, job)
    job.skills = extract_skills(job.title, job.description)

    assert "SQL" in job.skills or "sql" in [s.lower() for s in job.skills]
    assert rank([job], query, min_score=0.02), "still unfindable after hydration"


@respx.mock
async def test_a_url_with_no_posting_id_is_skipped_rather_than_guessed() -> None:
    job = _job("https://in.linkedin.com/jobs/view/some-slug-with-no-id")
    async with httpx.AsyncClient() as client:
        assert await LinkedInGuestSource().hydrate(client, job) is False
    assert job.description == ""


@respx.mock
async def test_an_empty_body_does_not_overwrite_with_junk() -> None:
    respx.get(DETAIL_URL).mock(
        return_value=httpx.Response(200, text='<div class="description__text">  </div>')
    )
    job = _job()
    async with httpx.AsyncClient() as client:
        assert await LinkedInGuestSource().hydrate(client, job) is False
    assert job.description == ""


@respx.mock
async def test_a_rate_limit_propagates_so_the_pipeline_can_stop_early() -> None:
    """Hydration is a second request per posting against the source most likely to start
    429ing. It must give up on the whole batch rather than hammer through it."""
    from jobradar.fetcher import Blocked

    respx.get(DETAIL_URL).mock(return_value=httpx.Response(429))
    async with httpx.AsyncClient() as client:
        with pytest.raises((Blocked, httpx.HTTPStatusError)):
            await LinkedInGuestSource().hydrate(client, _job())
