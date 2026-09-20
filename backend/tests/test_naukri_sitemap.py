"""Naukri reconstructed from its published sitemaps.

The boundary this source must hold is as important as the parsing: it reads only the
files Naukri publishes for crawlers, and never a job page. If a future change makes it
fetch /job-listings-... it has stopped being this thing.

The parsing risk is specific. Naukri's slugs put the job title and the employer next to
each other with no delimiter, so a guessed split yields plausible, wrong records — an
early version produced "Assistant Research Scientist" at "Optometry Pandorum
Technologies". Requiring the employer to match Naukri's own company index is what makes
the split decidable, and these tests pin that it stays required.
"""

from __future__ import annotations

import gzip

import httpx
import pytest
import respx

from jobradar.models import Query, Seniority
from jobradar.sources.naukri_sitemap import (
    COMPANY_SITEMAPS,
    NaukriSitemapSource,
    parse_job_url,
)

COMPANIES = {
    "marriott",
    "cult-fit",
    "muthoot-finance",
    "tata-consultancy-services",
    "select-source-international",
    "ey",
}

MOD = "2026-09-19T01:44:47.902+05:30"


def url(slug: str) -> str:
    return f"https://www.naukri.com/job-listings-{slug}"


# --------------------------------------------------------------------------- parsing


def test_title_and_employer_split_on_the_company_index() -> None:
    job = parse_job_url(
        url("food-beverage-sales-executive-marriott-new-delhi-2-to-7-years-190925012345"),
        MOD,
        COMPANIES,
    )
    assert job is not None
    assert job.title == "Food Beverage Sales Executive"
    assert job.company == "Marriott"
    assert job.location == "New Delhi"
    assert job.seniority == Seniority.MID  # from the 2-year minimum
    assert "Experience: 2-7 yrs" in job.tags
    assert job.posted_at is not None


def test_multi_word_employers_win_over_short_ones() -> None:
    """ "tata-consultancy-services" must beat any shorter tail that also matches."""
    job = parse_job_url(
        url("data-engineer-tata-consultancy-services-gurugram-delhi-ncr-6-to-11-years-19092501"),
        MOD,
        COMPANIES,
    )
    assert job is not None
    assert job.title == "Data Engineer"
    assert job.company == "Tata Consultancy Services"
    assert job.location == "Gurugram, Delhi NCR"


def test_an_unknown_employer_is_dropped_rather_than_guessed() -> None:
    """The failure this whole design exists to prevent. "Optometry" belongs to the title
    and "Pandorum Technologies" to the employer, and nothing in the URL says where the
    boundary is — so without the index, we decline."""
    assert (
        parse_job_url(
            url(
                "assistant-research-scientist-optometry-pandorum-technologies-new-delhi-2-to-3-years-1909"
            ),
            MOD,
            COMPANIES,
        )
        is None
    )


def test_a_listing_with_no_title_left_is_dropped() -> None:
    assert (
        parse_job_url(url("marriott-new-delhi-1-to-3-years-190925012345"), MOD, COMPANIES) is None
    )


def test_malformed_urls_are_ignored() -> None:
    for bad in (
        "https://www.naukri.com/data-scientist-jobs-in-delhi",
        url("no-experience-band-190925012345"),
        "https://www.naukri.com/job-listings-x-1-to-3-years-42",  # id too short
        "",
    ):
        assert parse_job_url(bad, MOD, COMPANIES) is None


def test_every_city_named_in_the_slug_is_kept() -> None:
    job = parse_job_url(
        url("data-analyst-ey-pune-bengaluru-delhi-ncr-5-to-8-years-190925012345"), MOD, COMPANIES
    )
    assert job is not None
    assert job.location == "Pune, Bengaluru, Delhi NCR"
    from jobradar.geo import matches_location

    # A posting listed in three cities has to answer a search for any of them.
    assert matches_location(job.location, "Delhi")
    assert matches_location(job.location, "Pune")
    assert matches_location(job.location, "Bangalore")


def test_cards_say_the_description_lives_on_naukri() -> None:
    """There is no description, because fetching one is the thing this source will not
    do. The card must say so rather than looking like a posting with nothing in it."""
    job = parse_job_url(
        url("dance-instructor-cult-fit-gurugram-1-to-4-years-19092501"), MOD, COMPANIES
    )
    assert job is not None
    assert job.description == ""
    assert "Details on Naukri" in job.tags


# ------------------------------------------------------------------------- fetching


def sitemap_xml(slugs: list[str]) -> str:
    entries = "".join(f"<url><loc>{url(s)}</loc><lastmod>{MOD}</lastmod></url>" for s in slugs)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset>{entries}</urlset>'


def company_xml(names: set[str]) -> bytes:
    entries = "".join(
        f"<url><loc>https://www.naukri.com/{n}-jobs-careers-{i}</loc></url>"
        for i, n in enumerate(sorted(names), start=1)
    )
    return gzip.compress(f"<urlset>{entries}</urlset>".encode())


@respx.mock
async def test_end_to_end_reads_only_sitemaps() -> None:
    respx.get(COMPANY_SITEMAPS[0]).mock(
        return_value=httpx.Response(200, content=company_xml(COMPANIES))
    )
    respx.get(COMPANY_SITEMAPS[1]).mock(
        return_value=httpx.Response(200, content=company_xml(set()))
    )
    city = respx.get("https://www.naukri.com/sitemap/jobDescPagesDelhi.xml").mock(
        return_value=httpx.Response(
            200,
            text=sitemap_xml(
                [
                    "food-beverage-sales-executive-marriott-new-delhi-2-to-7-years-190925012345",
                    "dance-instructor-cult-fit-gurugram-delhi-ncr-1-to-4-years-190925012346",
                    "some-role-unknown-employer-ltd-new-delhi-1-to-3-years-190925012347",
                ]
            ),
        )
    )
    # Any request for an actual job page is a bug: those are the pages Naukri defends.
    job_page = respx.get(url__regex=r".*/job-listings-.*\d{8,}$").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )

    src = NaukriSitemapSource({"cities": ["Delhi"]})
    async with httpx.AsyncClient() as client:
        jobs = await src.search(client, Query(keywords=[]))

    assert city.called
    assert not job_page.called, "this source must never fetch a Naukri job page"
    assert [j.company for j in jobs] == ["Marriott", "Cult Fit"]


@respx.mock
async def test_without_the_company_index_it_returns_nothing() -> None:
    """Rather than fall back to guessing the split."""
    for u in COMPANY_SITEMAPS:
        respx.get(u).mock(return_value=httpx.Response(500))
    city = respx.get("https://www.naukri.com/sitemap/jobDescPagesDelhi.xml").mock(
        return_value=httpx.Response(
            200, text=sitemap_xml(["x-marriott-new-delhi-1-to-3-years-19092501"])
        )
    )
    src = NaukriSitemapSource({"cities": ["Delhi"]})
    async with httpx.AsyncClient() as client:
        assert await src.search(client, Query(keywords=[])) == []
    assert not city.called


@respx.mock
async def test_a_missing_city_file_does_not_lose_the_others() -> None:
    respx.get(COMPANY_SITEMAPS[0]).mock(
        return_value=httpx.Response(200, content=company_xml(COMPANIES))
    )
    respx.get(COMPANY_SITEMAPS[1]).mock(return_value=httpx.Response(404))
    respx.get("https://www.naukri.com/sitemap/jobDescPagesDelhi.xml").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://www.naukri.com/sitemap/jobDescPagesPune.xml").mock(
        return_value=httpx.Response(
            200, text=sitemap_xml(["data-analyst-ey-pune-3-to-6-years-190925012345"])
        )
    )
    src = NaukriSitemapSource({"cities": ["Delhi", "Pune"]})
    async with httpx.AsyncClient() as client:
        jobs = await src.search(client, Query(keywords=[]))
    assert [j.company for j in jobs] == ["EY"]


@pytest.mark.parametrize("compressed", [True, False])
@respx.mock
async def test_company_index_handles_gzip_either_way(compressed: bool) -> None:
    """The company files are .gz, which arrives compressed even though httpx decodes
    Content-Encoding — but a proxy may have decompressed it already."""
    body = company_xml(COMPANIES) if compressed else gzip.decompress(company_xml(COMPANIES))
    respx.get(COMPANY_SITEMAPS[0]).mock(return_value=httpx.Response(200, content=body))
    respx.get(COMPANY_SITEMAPS[1]).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as client:
        found = await NaukriSitemapSource().company_index(client)
    assert "marriott" in found


def test_acronyms_survive_the_slug_round_trip() -> None:
    """A slug is all lowercase, so word-capitalising it published "Delhi Ncr" and "Ey"
    on 130 live listings."""
    job = parse_job_url(
        url("data-analyst-ey-noida-gurugram-delhi-ncr-5-to-8-years-190925012345"),
        MOD,
        COMPANIES,
    )
    assert job is not None
    assert job.company == "EY"
    assert job.location == "Noida, Gurugram, Delhi NCR"


def test_ordinary_words_are_not_shouted() -> None:
    job = parse_job_url(
        url("senior-research-analyst-marriott-new-delhi-2-to-7-years-190925012345"),
        MOD,
        COMPANIES,
    )
    assert job is not None
    assert job.title == "Senior Research Analyst"
    assert job.company == "Marriott"
