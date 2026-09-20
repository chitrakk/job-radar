"""Shine, Internshala and Wellfound adapters.

Each fixture is in the board's real embedding format: Shine's streamed Next.js flight
chunks, Wellfound's Apollo cache in __NEXT_DATA__, and two Internshala cards cut verbatim
from the live page. The traps each board sets are tested explicitly, because every one of
them produces plausible-looking wrong data rather than an error.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path

import httpx
import pytest
import respx

from jobradar.geo import matches_location
from jobradar.models import Query, RemoteKind, Seniority
from jobradar.sources.indian_boards import (
    InternshalaSource,
    ShineSource,
    WellfoundSource,
    _years_to_seniority,
    jobposting_from_html,
    slugify,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ============================================================================== Shine


SHINE_JOBS = [
    {
        "companyName": "EXL",
        "detailUrl": "/jobs/data-scientist/exl/19559001",
        "experienceLabel": "2 to 5 Yrs",
        "id": "19559001",
        "locations": ["Gurugram"],
        "postedAt": "2026-09-16T10:27:58",
        "salaryLabel": "12 - 14 Lakh/Yr",
        "skills": ["Python", "SQL", "Machine Learning"],
        "title": "Data Scientist",
    },
    {
        "companyName": "Northern Trust",
        "detailUrl": "/jobs/algorithmic-trading-data-scientist/northern-trust/19545832",
        "experienceLabel": "0 to 4 Yrs",
        "id": "19545832",
        "locations": ["Bangalore", "Chennai", "Noida", "Hyderabad", "Pune", "Mumbai City"],
        "postedAt": "2026-09-07T19:28:51",
        "salaryLabel": "Not Disclosed",
        "skills": ["Python", "Backtesting"],
        "title": "Algorithmic Trading Data Scientist — ₹ bonus",
    },
    {"companyName": "Broken", "title": "No URL at all"},
]


def shine_page(jobs: list[dict]) -> str:
    """Wrap a jobs array the way Shine streams it: split across flight chunks, each a
    JavaScript string literal. Splitting mid-object is deliberate — that is what the real
    page does, and a parser that reads one chunk at a time would miss every job."""
    payload = '1:{"pageCount":609,"totalCount":12169,"jobs":' + json.dumps(jobs) + "}\n"
    half = len(payload) // 2
    chunks = [payload[:half], payload[half:]]
    scripts = "".join(f"<script>self.__next_f.push([1,{json.dumps(c)}])</script>" for c in chunks)
    return f"<html><body>{scripts}</body></html>"


def test_shine_reads_jobs_split_across_flight_chunks() -> None:
    jobs = ShineSource().parse(shine_page(SHINE_JOBS))
    assert [j.title for j in jobs] == [
        "Data Scientist",
        "Algorithmic Trading Data Scientist — ₹ bonus",
    ]
    # Non-ASCII must survive; unicode_escape decoding would have mangled it.
    assert "₹" in jobs[1].title


def test_shine_fields() -> None:
    exl = ShineSource().parse(shine_page(SHINE_JOBS))[0]
    assert exl.url == "https://www.shine.com/jobs/data-scientist/exl/19559001"
    assert exl.location == "Gurugram, India"
    assert exl.skills == ["Python", "SQL", "Machine Learning"]
    assert exl.seniority == Seniority.MID
    assert exl.salary_currency == "INR" and exl.salary_max == pytest.approx(1_400_000)
    assert exl.salary_text == "12 - 14 Lakh/Yr"
    assert "Experience: 2 to 5 Yrs" in exl.tags


def test_shine_timestamps_are_ist_not_utc() -> None:
    """Shine's postedAt is naive local time. Treating it as UTC would date every posting
    five and a half hours into the future."""
    exl = ShineSource().parse(shine_page(SHINE_JOBS))[0]
    assert exl.posted_at is not None
    assert exl.posted_at.astimezone(UTC).hour == 4  # 10:27 IST


def test_shine_not_disclosed_is_no_salary_rather_than_zero() -> None:
    nt = ShineSource().parse(shine_page(SHINE_JOBS))[1]
    assert nt.salary_max is None and nt.salary_text == ""


def test_shine_multi_city_posting_answers_each_city() -> None:
    """A posting hiring in six cities including Noida must answer a Delhi search."""
    nt = ShineSource().parse(shine_page(SHINE_JOBS))[1]
    assert "+2 more" in nt.location
    assert matches_location(nt.location, "Noida")
    assert matches_location(nt.location, "Delhi")  # Noida is in the Delhi NCR market


def test_shine_urls() -> None:
    src = ShineSource({"pages": 2, "cities": ["delhi", "Navi Mumbai"]})
    assert src._urls("Data Scientist") == [
        "https://www.shine.com/job-search/data-scientist-jobs",
        # ?page=2 is silently ignored by Shine and returns page 1 again.
        "https://www.shine.com/job-search/data-scientist-jobs-2",
        "https://www.shine.com/job-search/data-scientist-jobs-in-delhi",
        "https://www.shine.com/job-search/data-scientist-jobs-in-navi-mumbai",
    ]


DETAIL = """
<html><head>
<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[]}</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",
 "title":"Data Scientist","datePosted":"2026-09-16T10:27:58+05:30",
 "description":"<p>Build <b>churn models</b> in Python and SQL for our Gurugram team.\
 You will own the full lifecycle from framing to deployment, with product and risk.</p>"}
</script></head><body></body></html>
"""


@respx.mock
async def test_shine_hydrate_reads_json_ld() -> None:
    job = ShineSource().parse(shine_page(SHINE_JOBS))[0]
    respx.get(job.url).mock(return_value=httpx.Response(200, text=DETAIL))
    async with httpx.AsyncClient() as client:
        assert await ShineSource().hydrate(client, job)
    assert "churn models" in job.description and "<b>" not in job.description


def test_json_ld_skips_non_job_blocks() -> None:
    assert jobposting_from_html(DETAIL)["title"] == "Data Scientist"
    assert jobposting_from_html("<html></html>") is None


# ======================================================================== Internshala


def test_internshala_parses_real_cards() -> None:
    jobs = InternshalaSource().parse((FIXTURES / "internshala.html").read_text())
    assert len(jobs) == 2
    first = jobs[0]
    assert first.title == "Data Analytics Executive"
    assert first.company == "Pepmedia Services"
    assert first.location == "Mumbai, India"
    assert first.url.startswith("https://internshala.com/job/detail/")
    assert first.salary_currency == "INR" and first.salary_max == pytest.approx(240_000)
    assert {"Python", "SQL"} <= set(first.skills)
    # Internshala cards carry the description inline; no second request needed.
    assert len(first.description) > 500
    assert first.posted_at is not None


def test_internshala_level_comes_from_experience_not_the_title() -> None:
    """Title inflation is common on Indian boards: this "Senior Manager" asks for one
    year at ₹4L/yr. The experience asked for is what decides who can apply."""
    jobs = InternshalaSource().parse((FIXTURES / "internshala.html").read_text())
    manager = next(j for j in jobs if "Senior Manager" in j.title)
    assert manager.seniority == Seniority.ENTRY


def test_internshala_skips_internships_in_a_job_listing() -> None:
    html = (
        (FIXTURES / "internshala.html")
        .read_text()
        .replace('employment_type="job"', 'employment_type="internship"', 1)
    )
    assert len(InternshalaSource().parse(html)) == 1


def test_internshala_work_from_home_is_remote() -> None:
    html = (
        (FIXTURES / "internshala.html")
        .read_text()
        .replace("<a>Mumbai</a>", "<a>Work from home</a>", 1)
    )
    assert InternshalaSource().parse(html)[0].remote == RemoteKind.REMOTE


def test_internshala_urls() -> None:
    src = InternshalaSource({"cities": ["Gurgaon"]})
    assert src._urls("data analyst") == [
        "https://internshala.com/jobs/keywords-data-analyst/",
        "https://internshala.com/jobs/data-analyst-jobs-in-gurgaon/",
    ]


# ========================================================================== Wellfound


def wellfound_page(listings: list[dict], startups: list[dict]) -> str:
    data: dict[str, dict] = {}
    for item in listings:
        data[f"JobListingSearchResult:{item['id']}"] = {
            "__typename": "JobListingSearchResult",
            **item,
        }
    for s in startups:
        data[f"StartupResult:{s['id']}"] = {"__typename": "StartupResult", **s}
    nd = {"props": {"pageProps": {"apolloState": {"data": data}}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'


WF_LISTINGS = [
    {
        "id": "4722684",
        "title": "Data Scientist",
        "slug": "data-scientist",
        "locationNames": ["Gurgaon"],
        "remote": False,
        "remoteConfig": {"kind": "ONSITE"},
        "compensation": "₹6L – ₹20L • 0.1% – 0.5%",
        "liveStartAt": 1789602099,
        "yearsExperienceMin": 2,
        "yearsExperienceMax": 5,
        "jobType": "full-time",
        "description": "## About\n\nWe build **credit models** for India's next 100M borrowers.",
    },
    {
        "id": "999",
        "title": "Senior Data Scientist",
        "slug": "senior-data-scientist",
        "locationNames": [],
        "remote": True,
        "remoteConfig": {"kind": "REMOTE"},
        "acceptedRemoteLocationNames": ["India"],
        "compensation": "",
        "liveStartAt": 1789602099,
    },
    # A listing no startup points at has no company; it must be dropped, not invented.
    {"id": "555", "title": "Orphan", "slug": "orphan", "locationNames": ["Pune"]},
]
WF_STARTUPS = [
    {
        "id": "1",
        "name": "Lendwise",
        "highlightedJobListings": [
            {"__ref": "JobListingSearchResult:4722684"},
            {"__ref": "JobListingSearchResult:999"},
        ],
    }
]


def test_wellfound_joins_listings_to_their_startup() -> None:
    jobs = WellfoundSource().parse(wellfound_page(WF_LISTINGS, WF_STARTUPS))
    assert [(j.title, j.company) for j in jobs] == [
        ("Data Scientist", "Lendwise"),
        ("Senior Data Scientist", "Lendwise"),
    ]


def test_wellfound_fields() -> None:
    ds, remote = WellfoundSource().parse(wellfound_page(WF_LISTINGS, WF_STARTUPS))
    assert ds.url == "https://wellfound.com/jobs/4722684-data-scientist"
    assert ds.location == "Gurgaon"
    assert ds.salary_currency == "INR" and ds.salary_max == pytest.approx(2_000_000)
    assert ds.seniority == Seniority.MID
    assert "credit models" in ds.description
    assert remote.remote == RemoteKind.REMOTE
    assert remote.location == "Remote (India)"
    assert matches_location(remote.location, "India", is_remote=True)


@respx.mock
async def test_wellfound_discards_a_redirect_to_a_broader_page() -> None:
    """/role/l/data-scientist/delhi is not a Wellfound place; it 301s to a worldwide Data
    Scientist list. Trusting that page would fill an India corpus with US roles."""
    respx.get("https://wellfound.com/role/l/data-scientist/delhi").mock(
        return_value=httpx.Response(
            301, headers={"Location": "https://wellfound.com/role/data-scientist"}
        )
    )
    respx.get("https://wellfound.com/role/data-scientist").mock(
        return_value=httpx.Response(200, text=wellfound_page(WF_LISTINGS, WF_STARTUPS))
    )
    src = WellfoundSource({"places": ["delhi"], "pages": 1})
    async with httpx.AsyncClient(follow_redirects=True) as client:
        jobs = await src.search(client, Query(keywords=["data scientist"]))
    assert jobs == []


@respx.mock
async def test_each_url_is_fetched_once_per_run() -> None:
    """One source instance serves every saved query, and keywords repeat across them."""
    route = respx.get("https://wellfound.com/role/l/data-analyst/india").mock(
        return_value=httpx.Response(200, text=wellfound_page(WF_LISTINGS, WF_STARTUPS))
    )
    src = WellfoundSource({"places": ["india"], "pages": 1})
    async with httpx.AsyncClient(follow_redirects=True) as client:
        await src.search(client, Query(keywords=["data analyst"]))
        await src.search(client, Query(keywords=["Data Analyst"]))
    assert route.call_count == 1


# ============================================================================ helpers


@pytest.mark.parametrize(
    ("label", "level"),
    [
        ("0 to 4 Yrs", Seniority.ENTRY),
        ("1 year(s)", Seniority.ENTRY),
        ("2 to 7 Yrs", Seniority.MID),
        ("5-8 years", Seniority.SENIOR),
        ("10+ Yrs", Seniority.LEAD),
        ("", Seniority.UNKNOWN),
    ],
)
def test_level_from_minimum_experience(label: str, level: Seniority) -> None:
    assert _years_to_seniority(label) == level


def test_slugify() -> None:
    assert slugify("Business Intelligence Analyst") == "business-intelligence-analyst"
    assert slugify("C++ / Python dev") == "c-python-dev"
