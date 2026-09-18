"""Tier 2 scraper parsing, against saved markup.

No live requests: these boards block datacenter IPs by design, so a test that hit them would
fail in CI for reasons unrelated to the code. What these tests pin down is the part that
actually breaks — the parsing, the block detection, and the promise that a blocked source
degrades a run instead of failing it.

Skipped entirely when the optional `scrape` extra is not installed.
"""

from __future__ import annotations

import pytest

pytest.importorskip("scrapling", reason="Tier 2 needs the optional [scrape] extra")

from scrapling import Selector  # noqa: E402

from jobradar.fetcher import Blocked  # noqa: E402
from jobradar.models import Query, Tier  # noqa: E402
from jobradar.sources.scraped import (  # noqa: E402
    FounditSource,
    IndeedIndiaSource,
    InstahyreSource,
    NaukriSource,
    ScrapedSource,
    TimesJobsSource,
    looks_blocked,
    relative_date,
)

NAUKRI_HTML = """
<html><body>
<div class="srp-jobtuple-wrapper">
  <a class="title" href="https://naukri.com/jl-data-analyst-1">Senior Data Analyst</a>
  <a class="comp-name" href="#">Acme Analytics</a>
  <span class="sal-wrap"><span>12-18 Lacs PA</span></span>
  <span class="locWdth">Bengaluru</span>
  <span class="job-desc">Build dashboards using SQL and Power BI</span>
  <span class="job-post-day">3 days ago</span>
</div>
<div class="srp-jobtuple-wrapper">
  <a class="title" href="https://naukri.com/jl-bi-dev-2">BI Developer</a>
  <a class="comp-name" href="#">Globex</a>
  <span class="locWdth">Hyderabad</span>
  <span class="job-post-day">Just now</span>
</div>
</body></html>
"""

TIMESJOBS_HTML = """
<html><body>
<li class="clearfix job-bx">
  <h2><a href="https://www.timesjobs.com/job-detail/1">Business Analyst</a></h2>
  <h3 class="joblist-comp-name">Initech Pvt Ltd</h3>
  <ul class="top-jd-dtl"><li>8-12 Lakhs</li><li>Pune</li></ul>
  <ul class="list-job-dtl"><li>Own the reporting stack</li></ul>
  <span class="sim-posted"><span>Posted 5 days ago</span></span>
</li>
</body></html>
"""

FOUNDIT_HTML = """
<html><body>
<div class="srpResultCard">
  <div class="jobTitle">Data Engineer</div>
  <a href="/job/data-engineer-1">link</a>
  <div class="companyName">Umbrella Corp</div>
  <div class="details location">Chennai</div>
  <div class="jobDescription">Pipelines in Python and Spark</div>
  <span class="timeText">2 days ago</span>
</div>
</body></html>
"""

INSTAHYRE_JSON = """
<html><body><pre>{"objects": [
 {"title": "Product Analyst",
  "employer": {"company_name": "Zeta"},
  "locations": [{"name": "Bengaluru"}],
  "public_url": "/c/zeta/jobs/product-analyst-1",
  "description": "<p>SQL and experimentation</p>",
  "min_salary": 15, "max_salary": 25}
]}</pre></body></html>
"""


class FakePage:
    """Stands in for a Scrapling Response: the parsers only use .css and .html_content."""

    def __init__(self, html: str, status: int = 200) -> None:
        self._sel = Selector(html)
        self.html_content = html
        self.status = status

    def css(self, selector: str):
        return self._sel.css(selector)


# --------------------------------------------------------------------------- block detection


@pytest.mark.parametrize(
    ("html", "status", "expected"),
    [
        ("<html>" + "x" * 900 + "</html>", 200, False),
        ("<html><title>Just a moment...</title>" + "x" * 900 + "</html>", 200, True),
        ("<html>" + "x" * 900 + "Please enable JavaScript and cookies</html>", 200, True),
        ("<html>" + "x" * 900 + "</html>", 406, True),  # Naukri's datacenter-IP refusal
        ("<html>" + "x" * 900 + "</html>", 403, True),  # Cloudflare
        ("", 200, True),  # empty body is a block, not an empty result set
        ("<html>tiny</html>", 200, True),
    ],
)
def test_block_detection(html: str, status: int, expected: bool) -> None:
    """A challenge page parses to zero jobs and would otherwise look like 'no results'.
    Detecting it is what makes the health report honest."""
    assert looks_blocked(html, status) is expected


@pytest.mark.parametrize(
    ("text", "max_age_days"),
    [
        ("Just now", 0),
        ("1 day ago", 1),
        ("3 days ago", 3),
        ("2 weeks ago", 15),
        ("30+ days ago", 31),
    ],
)
def test_relative_date_parsing(text: str, max_age_days: int) -> None:
    from datetime import UTC, datetime

    parsed = relative_date(text)
    assert parsed is not None
    age = (datetime.now(UTC) - parsed).days
    assert age <= max_age_days + 1


def test_relative_date_ignores_unparseable() -> None:
    assert relative_date("") is None
    assert relative_date("sometime recently") is None


# --------------------------------------------------------------------------- parsers


def test_naukri_parses_cards_and_indian_salary() -> None:
    jobs = NaukriSource().parse(FakePage(NAUKRI_HTML), "https://naukri.com")

    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Senior Data Analyst"
    assert first.company == "Acme Analytics"
    assert first.location == "Bengaluru"
    # "12-18 Lacs PA" must normalise to annual rupees, not be dropped.
    assert first.salary_min == 1_200_000
    assert first.salary_max == 1_800_000
    assert first.salary_currency == "INR"
    assert first.posted_at is not None
    # A card with no salary or description still yields a usable job.
    assert jobs[1].title == "BI Developer"
    assert jobs[1].salary_min is None


def test_timesjobs_parses() -> None:
    jobs = TimesJobsSource().parse(FakePage(TIMESJOBS_HTML), "https://timesjobs.com")
    assert len(jobs) == 1
    assert jobs[0].title == "Business Analyst"
    assert jobs[0].company == "Initech Pvt Ltd"
    assert jobs[0].salary_currency == "INR"


def test_foundit_parses_and_builds_absolute_urls() -> None:
    jobs = FounditSource().parse(FakePage(FOUNDIT_HTML), "https://foundit.in")
    assert len(jobs) == 1
    assert jobs[0].title == "Data Engineer"
    assert jobs[0].url.startswith("https://www.foundit.in/")


def test_instahyre_parses_json_payload() -> None:
    jobs = InstahyreSource().parse(FakePage(INSTAHYRE_JSON), "https://instahyre.com")
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Product Analyst"
    assert job.company == "Zeta"
    assert job.url == "https://www.instahyre.com/c/zeta/jobs/product-analyst-1"
    assert job.salary_min == 1_500_000  # 15 LPA
    assert "<p>" not in job.description


def test_parsers_return_empty_on_unrecognised_markup() -> None:
    """A site redesign should yield zero jobs, not an exception that kills the run."""
    blank = FakePage("<html><body><div>totally different now</div></body></html>")
    for source in (NaukriSource(), IndeedIndiaSource(), FounditSource(), TimesJobsSource()):
        assert source.parse(blank, "https://x") == []


def test_instahyre_survives_malformed_json() -> None:
    assert InstahyreSource().parse(FakePage("<pre>{not json</pre>"), "https://x") == []


# --------------------------------------------------------------------------- behaviour


def test_tier2_sources_are_tier2_and_disabled_by_default() -> None:
    """They must stay off until someone has a residential proxy, or every cloud run fills
    the health report with expected failures."""
    for cls in (NaukriSource, IndeedIndiaSource, FounditSource, TimesJobsSource, InstahyreSource):
        assert cls.tier is Tier.TIER2
        assert cls.enabled is False
        # Far slower than Tier 1: these are sites we are a guest on, not APIs for us.
        assert cls.min_interval >= 5.0


def test_search_urls_include_the_query_and_location() -> None:
    query = Query(keywords=["data analyst"], location="Bengaluru")
    for source in (NaukriSource(), IndeedIndiaSource(), FounditSource(), TimesJobsSource()):
        urls = source.search_urls(query)
        assert urls, f"{source.name} produced no URLs"
        joined = " ".join(urls).lower()
        assert "data" in joined and "analyst" in joined
        assert "bengaluru" in joined


async def test_blocked_everywhere_raises_blocked_not_empty(monkeypatch) -> None:
    """The distinction that matters: 'we were refused' must not read as 'no jobs found'."""

    async def refuse(self, url):  # noqa: ANN001, ARG001
        return FakePage("<html><title>Just a moment...</title>" + "x" * 900 + "</html>")

    monkeypatch.setattr(ScrapedSource, "_fetch", refuse)
    source = NaukriSource()
    source.min_interval = 0

    with pytest.raises(Blocked, match="refused"):
        await source.search(None, Query(keywords=["analyst"]))


async def test_a_blocked_source_degrades_the_run_rather_than_failing_it(monkeypatch) -> None:
    async def refuse(self, url):  # noqa: ANN001, ARG001
        return FakePage("", status=406)

    monkeypatch.setattr(ScrapedSource, "_fetch", refuse)
    source = NaukriSource()
    source.min_interval = 0

    jobs, health = await source.run(None, Query(keywords=["analyst"]))

    assert jobs == []
    assert health.ok is False
    assert health.tier is Tier.TIER2
    assert "blocked" in health.error


async def test_partial_success_is_kept(monkeypatch) -> None:
    """One blocked page among several must not discard the pages that worked."""
    calls = {"n": 0}

    async def sometimes(self, url):  # noqa: ANN001, ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            return FakePage("<html><title>Just a moment...</title>" + "x" * 900 + "</html>")
        return FakePage(NAUKRI_HTML)

    monkeypatch.setattr(ScrapedSource, "_fetch", sometimes)
    source = NaukriSource()
    source.min_interval = 0

    jobs, health = await source.run(None, Query(keywords=["data analyst", "business analyst"]))

    assert health.ok is True
    assert len(jobs) >= 2
    assert all(j.source_tier is Tier.TIER2 for j in jobs)


async def test_a_fetch_exception_is_survivable(monkeypatch) -> None:
    async def explode(self, url):  # noqa: ANN001, ARG001
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(ScrapedSource, "_fetch", explode)
    source = TimesJobsSource()
    source.min_interval = 0

    jobs, health = await source.run(None, Query(keywords=["analyst"]))
    assert jobs == []
    assert health.ok is False
