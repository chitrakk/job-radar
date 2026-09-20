"""Naukri, reconstructed from the sitemaps Naukri publishes for crawlers.

Naukri is the largest Indian job board and was the biggest gap in this corpus. It is also
the most aggressively defended: measured from both a residential IP and a GitHub Actions
runner, its search HTML is a bot-trap shell (a 99999x99999 invisible overlay, no job
content), `jobapi/v3/search` answers `{"message":"recaptcha required"}`, and a headless
browser is refused harder than a plain request. Its robots.txt separately names and blocks
AI crawlers — claudebot, Claude-User, GPTBot, PerplexityBot — with Disallow: /.

Reading the job pages anyway would mean defeating Akamai's fingerprinting and a captcha.
That is circumventing an access control the operator deliberately runs, so this module
does not do it and must not be extended to. No job page is ever fetched here.

What it reads instead is what Naukri publishes *so that* crawlers will read it, all of
which returns 200 to an ordinary request:

    sitemap/jobDescPages{City}.xml   ~15,000 job URLs per city, refreshed daily
    sitemap/jobByCompany-{1,2}.xml.gz   37,000 employer names

Naukri's job URLs are structured, but with no delimiter between the job title and the
employer:

    /job-listings-food-beverage-sales-executive-marriott-new-delhi-2-to-7-years-190925012345

Splitting that by guesswork produced "Assistant Research Scientist" at a company called
"Optometry Pandorum Technologies", and "Application Developer" at "Google Cloud Fullstack
Ibm" — plausible-looking and wrong, which is the worst kind of data. So the employer
dictionary decides it: a listing is only emitted when the tail of its slug matches a
company Naukri itself lists, and everything before that is the title. That is right when
it fires and silent when it does not — about a quarter of listings, the rest dropped.

What you get: title, employer, cities, experience band, posted date and a link.
What you do not get: description or salary, which exist only on the page. Cards from this
source say so, and the link opens normally in a human's browser.
"""

from __future__ import annotations

import gzip
import logging
import re
from datetime import UTC, datetime

import httpx

from ..fetcher import fetch
from ..geo import canonical_city
from ..models import Job, Query, Tier
from .base import Source, register
from .indian_boards import _years_to_seniority

log = logging.getLogger(__name__)

SITEMAP = "https://www.naukri.com/sitemap/jobDescPages{city}.xml"
COMPANY_SITEMAPS = [
    "https://www.naukri.com/sitemap/jobByCompany-1.xml.gz",
    "https://www.naukri.com/sitemap/jobByCompany-2.xml.gz",
]

# The city files Naukri publishes. There is no Gurugram file; Gurugram postings appear in
# the Delhi and Noida ones, which is also how Naukri's own NCR pages behave.
CITY_FILES = ["Delhi", "Noida", "Bangalore", "Chennai", "Pune", "Kolkata", "Ahmedabad"]

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")
_ENTRY = re.compile(
    r"<loc>\s*(?P<url>[^<\s]+)\s*</loc>\s*<lastmod>\s*(?P<mod>[^<\s]+)\s*</lastmod>"
)
_COMPANY_URL = re.compile(r"naukri\.com/(?P<slug>.+?)-jobs-careers-\d+/?$")
_JOB_URL = re.compile(
    r"/job-listings-(?P<slug>.+?)-(?P<lo>\d+)-to-(?P<hi>\d+)-years?-(?P<id>\d{8,})/?$"
)

# Place words that trail a Naukri slug, matched longest-first so "delhi-ncr" is consumed
# before "delhi" and "greater-noida" before "noida".
PLACE_WORDS = sorted(
    [
        "delhi-ncr",
        "new-delhi",
        "greater-noida",
        "navi-mumbai",
        "bangalore-rural",
        "bangalore-urban",
        "hyderabad",
        "secunderabad",
        "bengaluru",
        "bangalore",
        "gurugram",
        "gurgaon",
        "noida",
        "ghaziabad",
        "faridabad",
        "chennai",
        "kolkata",
        "mumbai",
        "pune",
        "ahmedabad",
        "jaipur",
        "chandigarh",
        "mohali",
        "kochi",
        "coimbatore",
        "indore",
        "lucknow",
        "nagpur",
        "surat",
        "vadodara",
        "bhubaneswar",
        "thiruvananthapuram",
        "mysuru",
        "mysore",
        "visakhapatnam",
        "patna",
        "bhopal",
        "goa",
        "guwahati",
        "raipur",
        "remote",
        "india",
        "anywhere",
    ],
    key=len,
    reverse=True,
)

# How many trailing slug words may form an employer name.
MAX_COMPANY_WORDS = 8


def _strip_trailing_places(parts: list[str]) -> tuple[list[str], list[str]]:
    """Peel city words off the end of a slug. Returns (remaining, places)."""
    places: list[str] = []
    changed = True
    while changed and parts:
        changed = False
        for word in PLACE_WORDS:
            bits = word.split("-")
            if len(parts) >= len(bits) and parts[-len(bits) :] == bits:
                places.insert(0, word)
                del parts[-len(bits) :]
                changed = True
                break
    return parts, places


# A slug is all lowercase, so capitalising each word turns "delhi-ncr" into "Delhi Ncr" and
# "ey" into "Ey". These are the tokens where that is visibly wrong in Indian job data.
ACRONYMS = {
    "ncr",
    "ey",
    "ibm",
    "tcs",
    "hcl",
    "kpmg",
    "pwc",
    "hdfc",
    "icici",
    "sbi",
    "hsbc",
    "hr",
    "it",
    "ites",
    "bpo",
    "kpo",
    "bfsi",
    "nbfc",
    "mnc",
    "sme",
    "msme",
    "ai",
    "ml",
    "sql",
    "aws",
    "gcp",
    "sap",
    "crm",
    "erp",
    "qa",
    "ui",
    "ux",
    "seo",
    "llp",
    "uae",
    "usa",
    "uk",
    "gst",
    "kyc",
    "cfa",
    "mba",
    "bsc",
    "msc",
    "bca",
    "mca",
}


def _titlecase(slug: str) -> str:
    words = []
    for w in slug.split("-"):
        if w.isupper():
            words.append(w)
        elif w.lower() in ACRONYMS:
            words.append(w.upper())
        else:
            words.append(w.capitalize())
    return " ".join(words)


def parse_job_url(url: str, lastmod: str, companies: set[str]) -> Job | None:
    """Reconstruct one posting from its URL, or None when the split is not certain."""
    m = _JOB_URL.search(url)
    if not m:
        return None

    parts = m.group("slug").split("-")
    parts, places = _strip_trailing_places(parts)

    # The longest trailing run of words that Naukri itself lists as an employer. Requiring
    # a match is the whole point: without it the title/company boundary is a guess.
    company_slug = ""
    for n in range(min(MAX_COMPANY_WORDS, len(parts) - 1), 0, -1):
        candidate = "-".join(parts[-n:])
        if candidate in companies:
            company_slug = candidate
            break
    if not company_slug:
        return None

    title_parts = parts[: -len(company_slug.split("-"))]
    title = _titlecase("-".join(title_parts)).strip()
    if len(title) < 3:
        return None

    location = ", ".join(_titlecase(p) for p in places) or "India"
    if not canonical_city(location) and "india" not in location.lower():
        location = f"{location}, India"

    posted = None
    try:
        posted = datetime.fromisoformat(lastmod).astimezone(UTC)
    except ValueError:
        pass

    lo, hi = m.group("lo"), m.group("hi")
    job = Job(
        title=title,
        company=_titlecase(company_slug),
        location=location,
        url=url,
        posted_at=posted,
        seniority=_years_to_seniority(lo),
    )
    job.tags.append(f"Experience: {lo}-{hi} yrs")
    # The UI shows this instead of pretending the posting had no description.
    job.tags.append("Details on Naukri")
    return job


@register
class NaukriSitemapSource(Source):
    name = "naukri_sitemap"
    tier = Tier.TIER2
    min_interval = 3.0
    # Whole-city dumps that ignore the query, so one pass per run is enough; the
    # pipeline's relevance filter decides what is worth keeping.
    query_independent = True

    @property
    def cities(self) -> list[str]:
        return [str(c) for c in self.config.get("cities", CITY_FILES) or CITY_FILES]

    async def _fetch_bytes(self, client: httpx.AsyncClient, url: str) -> str:
        resp = await fetch(
            client,
            url,
            min_interval=self.min_interval,
            retries=1,
            headers={"Accept": "application/xml,text/xml", "Accept-Language": "en-IN"},
        )
        raw = resp.content
        # httpx transparently decodes Content-Encoding, but these are .gz *files*, which
        # arrive still compressed.
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", errors="ignore")

    async def company_index(self, client: httpx.AsyncClient) -> set[str]:
        """Employer slugs Naukri publishes. Without these nothing can be parsed."""
        companies: set[str] = set()
        for url in COMPANY_SITEMAPS:
            try:
                xml = await self._fetch_bytes(client, url)
            except (httpx.HTTPError, OSError) as exc:
                log.warning("naukri company sitemap %s unavailable: %s", url, exc)
                continue
            for loc in _LOC.findall(xml):
                if m := _COMPANY_URL.search(loc):
                    companies.add(m.group("slug"))
        return companies

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        companies = await self.company_index(client)
        if not companies:
            # Guessing the title/company split is worse than returning nothing.
            log.warning("naukri: no company index, skipping")
            return []
        log.info("naukri: %d employers in the index", len(companies))

        jobs: list[Job] = []
        for city in self.cities:
            try:
                xml = await self._fetch_bytes(client, SITEMAP.format(city=city))
            except (httpx.HTTPError, OSError) as exc:
                log.warning("naukri sitemap %s unavailable: %s", city, exc)
                continue
            jobs.extend(self.parse(xml, companies))
        return jobs

    def parse(self, xml: str, companies: set[str]) -> list[Job]:
        out: list[Job] = []
        for m in _ENTRY.finditer(xml):
            job = parse_job_url(m.group("url"), m.group("mod"), companies)
            if job:
                out.append(job)
        return out
