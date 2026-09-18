"""Tier 2 — Indian job boards, scraped with Scrapling.

These are the sites that matter most for India-domestic roles and are also the hardest to
reach. Research and testing both confirm the problem: Naukri answers datacenter IPs with
HTTP 406, and Indeed India runs Cloudflare Turnstile. GitHub Actions runners and every free
PaaS are datacenter IPs, so **these adapters are expected to report as blocked in the cloud**
and are disabled by default in config/sources.yml.

They are still worth having, for three reasons:
  1. They work from a residential IP — run the pipeline on your own machine and they help.
  2. Setting JOBRADAR_PROXY_URL to a residential proxy turns them on with no code change.
  3. A blocked source is reported honestly in the UI rather than silently thinning the list.

Scrapling does the heavy lifting: StealthyFetcher applies real browser TLS fingerprints and
can solve Cloudflare interstitials, and its adaptive selectors survive the layout changes
these sites ship constantly. It is an optional dependency — install with:

    uv pip install -e ".[scrape]" && scrapling install

Without it this module raises ImportError, sources/__init__.py catches that, and Tier 1
carries on unaffected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

import httpx

# Import-guarded: the whole module is optional. sources/__init__.py expects the ImportError.
from scrapling.fetchers import StealthyFetcher  # noqa: F401

from ..fetcher import Blocked, proxy_url
from ..models import Job, Query, Tier
from ..textutil import detect_remote, html_to_text, parse_salary, truncate
from .base import Source, register

log = logging.getLogger(__name__)

# Signals that we got a challenge or a block page rather than results. Checked before
# parsing, because a Cloudflare page parses to zero jobs and would otherwise look like
# "this search had no results" — the exact silent degradation we are trying to avoid.
BLOCK_MARKERS = (
    "just a moment",
    "checking your browser",
    "cf-challenge",
    "captcha",
    "recaptcha",
    "access denied",
    "unusual traffic",
    "enable javascript and cookies",
)


def looks_blocked(html: str, status: int | None = None) -> bool:
    if status in (403, 406, 429, 451):
        return True
    if not html or len(html) < 500:
        return True
    low = html[:4000].lower()
    return any(marker in low for marker in BLOCK_MARKERS)


def relative_date(text: str) -> datetime | None:
    """Indian boards write "3 days ago", "Just now", "30+ days ago"."""
    if not text:
        return None
    low = text.lower()
    if "just now" in low or "today" in low:
        return datetime.now(UTC)
    if "yesterday" in low:
        return datetime.now(UTC) - timedelta(days=1)
    m = re.search(r"(\d+)\s*\+?\s*(minute|hour|day|week|month)", low)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {
        "minute": timedelta(minutes=n),
        "hour": timedelta(hours=n),
        "day": timedelta(days=n),
        "week": timedelta(weeks=n),
        "month": timedelta(days=30 * n),
    }[unit]
    return datetime.now(UTC) - delta


class ScrapedSource(Source):
    """Shared Scrapling plumbing for Tier 2 boards."""

    tier = Tier.TIER2
    # Much slower than Tier 1. These are sites we are a guest on, not APIs published for us.
    min_interval = 6.0
    enabled = False
    # Whether this site needs a real browser (Cloudflare) or just a stealthy HTTP request.
    needs_browser = True
    solve_cloudflare = True

    def search_urls(self, query: Query) -> list[str]:
        raise NotImplementedError

    def parse(self, page: Any, url: str) -> list[Job]:
        raise NotImplementedError

    async def _fetch(self, url: str) -> Any:
        """Run Scrapling's blocking fetcher off the event loop.

        StealthyFetcher drives a real browser and blocks; calling it directly would stall
        every other source running concurrently in the same pipeline run.
        """
        kwargs: dict[str, Any] = {
            "headless": True,
            "network_idle": True,
            "timeout": 45000,
        }
        if self.solve_cloudflare:
            kwargs["solve_cloudflare"] = True
        if proxy := proxy_url():
            kwargs["proxy"] = proxy
        # Ask for an India-localised page where the site varies by geography.
        kwargs["geoip"] = True

        def run() -> Any:
            return StealthyFetcher.fetch(url, **kwargs)

        return await asyncio.to_thread(run)

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        jobs: list[Job] = []
        blocked = 0
        urls = self.search_urls(query)

        for i, url in enumerate(urls):
            if i:
                await asyncio.sleep(self.min_interval)
            try:
                page = await self._fetch(url)
            except Exception as exc:  # noqa: BLE001 - browser faults are routine here
                log.debug("%s fetch failed for %s: %s", self.name, url, exc)
                blocked += 1
                continue

            html = getattr(page, "html_content", "") or str(getattr(page, "body", "") or "")
            if looks_blocked(html, getattr(page, "status", None)):
                blocked += 1
                continue

            try:
                jobs.extend(self.parse(page, url))
            except Exception as exc:  # noqa: BLE001 - a layout change must not kill the run
                log.warning("%s parse failed: %s", self.name, exc)

        if blocked and not jobs:
            raise Blocked(
                f"{blocked}/{len(urls)} requests refused — expected from a datacenter IP. "
                "Set JOBRADAR_PROXY_URL to a residential proxy to use this source."
            )
        return jobs

    # -------------------------------------------------------------- helpers

    # Scrapling's Selector has no `css_first`; `.css(sel)` returns a Selectors list whose
    # `.first` is None when nothing matched. Both helpers swallow errors because these
    # selectors target sites that change their markup without notice, and a failed lookup
    # should yield an empty field rather than lose the whole page.

    @staticmethod
    def text(node: Any, selector: str, default: str = "") -> str:
        try:
            found = node.css(selector).first
            return found.get_all_text(strip=True) if found is not None else default
        except Exception:  # noqa: BLE001
            return default

    @staticmethod
    def attr(node: Any, selector: str, name: str, default: str = "") -> str:
        try:
            found = node.css(selector).first
            return (found.attrib.get(name) or default) if found is not None else default
        except Exception:  # noqa: BLE001
            return default


@register
class NaukriSource(ScrapedSource):
    """Naukri.com — India's largest job board.

    Naukri's own frontend calls a JSON API, which is far more stable than its markup, so we
    read that through the browser session rather than scraping rendered cards. The API
    rejects datacenter IPs with 406, which is the block this whole tier is gated behind.
    """

    name = "naukri"
    min_interval = 8.0

    def search_urls(self, query: Query) -> list[str]:
        loc = query.location or "India"
        out = []
        for kw in (query.keywords or [""])[:3]:
            slug = re.sub(r"[^a-z0-9]+", "-", kw.lower()).strip("-") or "jobs"
            out.append(
                f"https://www.naukri.com/{slug}-jobs-in-{quote_plus(loc.lower())}"
                f"?k={quote_plus(kw)}&l={quote_plus(loc)}"
            )
        return out

    def parse(self, page: Any, url: str) -> list[Job]:
        jobs: list[Job] = []

        # Preferred path: the embedded state blob the SPA hydrates from.
        for script in page.css("script"):
            raw = script.get_all_text() or ""
            if "jobDetails" not in raw and "jobTuple" not in raw:
                continue
            for match in re.finditer(r"\{[^{}]*\"jobId\"\s*:\s*\"[^\"]+\"[^{}]*\}", raw):
                try:
                    row = json.loads(match.group(0))
                except json.JSONDecodeError:
                    continue
                title = row.get("title") or row.get("jobTitle") or ""
                if not title:
                    continue
                jobs.append(
                    Job(
                        title=title,
                        company=row.get("companyName") or "Unknown",
                        location=row.get("placeholders", {}).get("location", "")
                        or row.get("location", ""),
                        url=row.get("jdURL") or row.get("jobUrl") or url,
                        description=truncate(html_to_text(row.get("jobDescription", ""))),
                        posted_at=relative_date(row.get("footerPlaceholderLabel", "")),
                        **parse_salary(str(row.get("placeholders", {}).get("salary", ""))),
                    )
                )
        if jobs:
            return jobs

        # Fallback: rendered cards. Naukri renames these classes often, which is exactly
        # what Scrapling's adaptive matching is for.
        for card in page.css("div.srp-jobtuple-wrapper, article.jobTuple"):
            title = self.text(card, "a.title")
            link = self.attr(card, "a.title", "href")
            if not (title and link):
                continue
            salary = self.text(card, "span.sal-wrap, span.salary")
            loc = self.text(card, "span.locWdth, span.location")
            desc = self.text(card, "span.job-desc, ul.job-desc")
            jobs.append(
                Job(
                    title=title,
                    company=self.text(card, "a.comp-name, a.subTitle") or "Unknown",
                    location=loc,
                    url=link,
                    description=truncate(desc),
                    posted_at=relative_date(self.text(card, "span.job-post-day, span.fleft")),
                    remote=detect_remote(loc, title, desc),
                    **parse_salary(salary),
                )
            )
        return jobs


@register
class IndeedIndiaSource(ScrapedSource):
    """Indeed India. Behind Cloudflare Turnstile, so this needs the full browser path."""

    name = "indeed_in"
    min_interval = 10.0

    def search_urls(self, query: Query) -> list[str]:
        loc = query.location or "India"
        return [
            f"https://in.indeed.com/jobs?q={quote_plus(kw)}&l={quote_plus(loc)}&fromage="
            f"{min(query.max_age_days, 30)}"
            for kw in (query.keywords or [""])[:3]
        ]

    def parse(self, page: Any, url: str) -> list[Job]:
        jobs: list[Job] = []

        # Indeed embeds its result set as JSON; parsing that beats fighting the markup.
        for script in page.css("script"):
            raw = script.get_all_text() or ""
            if "jobmap" not in raw and "mosaic-provider-jobcards" not in raw:
                continue
            for match in re.finditer(r'"jobkey"\s*:\s*"([a-f0-9]+)"', raw):
                key = match.group(1)
                window = raw[max(0, match.start() - 1200) : match.start() + 1200]
                title = re.search(r'"(?:title|jobTitle)"\s*:\s*"([^"]{3,140})"', window)
                company = re.search(r'"(?:company|companyName)"\s*:\s*"([^"]{1,120})"', window)
                loc = re.search(
                    r'"(?:formattedLocation|jobLocationCity)"\s*:\s*"([^"]{1,90})"', window
                )
                if not title:
                    continue
                jobs.append(
                    Job(
                        title=title.group(1),
                        company=company.group(1) if company else "Unknown",
                        location=loc.group(1) if loc else "",
                        url=f"https://in.indeed.com/viewjob?jk={key}",
                        remote=detect_remote(loc.group(1) if loc else "", title.group(1)),
                    )
                )
        if jobs:
            return jobs

        for card in page.css("div.job_seen_beacon, div.cardOutline"):
            title = self.text(card, "h2.jobTitle span[title], h2.jobTitle")
            link = self.attr(card, "h2.jobTitle a, a.jcs-JobTitle", "href")
            if not title:
                continue
            loc = self.text(card, "div[data-testid='text-location'], div.companyLocation")
            snippet = self.text(card, "div.job-snippet, div[data-testid='belowJobSnippet']")
            jobs.append(
                Job(
                    title=title,
                    company=self.text(card, "span[data-testid='company-name'], span.companyName")
                    or "Unknown",
                    location=loc,
                    url=("https://in.indeed.com" + link) if link.startswith("/") else (link or url),
                    description=truncate(snippet),
                    posted_at=relative_date(
                        self.text(card, "span.date, span[data-testid='myJobsStateDate']")
                    ),
                    remote=detect_remote(loc, title, snippet),
                    **parse_salary(
                        self.text(
                            card,
                            "div.salary-snippet-container, div.metadata.salary-snippet-container",
                        )
                    ),
                )
            )
        return jobs


@register
class FounditSource(ScrapedSource):
    """Foundit (formerly Monster India)."""

    name = "foundit"
    min_interval = 6.0

    def search_urls(self, query: Query) -> list[str]:
        loc = query.location or "india"
        return [
            f"https://www.foundit.in/srp/results?query={quote_plus(kw)}&locations={quote_plus(loc)}"
            for kw in (query.keywords or [""])[:2]
        ]

    def parse(self, page: Any, url: str) -> list[Job]:
        jobs = []
        for card in page.css("div.srpResultCard, div.cardContainer"):
            title = self.text(card, "div.jobTitle, h3.jobTitle")
            link = self.attr(card, "a", "href")
            if not title:
                continue
            loc = self.text(card, "div.details.location, span.location")
            desc = self.text(card, "div.jobDescription, p.jobDesc")
            jobs.append(
                Job(
                    title=title,
                    company=self.text(card, "div.companyName, span.companyName") or "Unknown",
                    location=loc,
                    url=link if link.startswith("http") else f"https://www.foundit.in{link}",
                    description=truncate(desc),
                    posted_at=relative_date(self.text(card, "span.timeText, div.timeStamp")),
                    remote=detect_remote(loc, title, desc),
                    **parse_salary(self.text(card, "div.details.salary, span.salary")),
                )
            )
        return jobs


@register
class TimesJobsSource(ScrapedSource):
    """TimesJobs. Server-rendered, so a stealthy HTTP request is usually enough."""

    name = "timesjobs"
    min_interval = 5.0
    solve_cloudflare = False

    def search_urls(self, query: Query) -> list[str]:
        return [
            "https://www.timesjobs.com/candidate/job-search.html?searchType=personalizedSearch"
            f"&from=submit&txtKeywords={quote_plus(kw)}"
            f"&txtLocation={quote_plus(query.location or 'India')}"
            for kw in (query.keywords or [""])[:2]
        ]

    def parse(self, page: Any, url: str) -> list[Job]:
        jobs = []
        for card in page.css("li.clearfix.job-bx"):
            title = self.text(card, "h2 a")
            link = self.attr(card, "h2 a", "href")
            if not title:
                continue
            loc = self.text(card, "ul.top-jd-dtl li:nth-child(2), span.loc")
            desc = self.text(card, "ul.list-job-dtl li")
            jobs.append(
                Job(
                    title=title,
                    company=self.text(card, "h3.joblist-comp-name") or "Unknown",
                    location=loc,
                    url=link or url,
                    description=truncate(desc),
                    posted_at=relative_date(self.text(card, "span.sim-posted span")),
                    remote=detect_remote(loc, title, desc),
                    **parse_salary(self.text(card, "ul.top-jd-dtl li:first-child")),
                )
            )
        return jobs


@register
class InstahyreSource(ScrapedSource):
    """Instahyre — curated Indian startup and tech roles. Has a public JSON endpoint."""

    name = "instahyre"
    min_interval = 5.0
    solve_cloudflare = False

    def search_urls(self, query: Query) -> list[str]:
        return [
            "https://www.instahyre.com/api/v1/job_search?limit=40&offset=0"
            f"&job_title={quote_plus(kw)}"
            for kw in (query.keywords or [""])[:2]
        ]

    def parse(self, page: Any, url: str) -> list[Job]:
        raw = getattr(page, "html_content", "") or str(getattr(page, "body", "") or "")
        # The API answer is JSON, but fetched through a browser it arrives wrapped in a
        # <pre> tag, so pull the outermost JSON object out of whatever came back.
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

        jobs = []
        for row in data.get("objects", []) or []:
            employer = row.get("employer") or {}
            title = row.get("title", "")
            if not title:
                continue
            desc = html_to_text(row.get("description", "") or row.get("job_description", ""))
            loc = ", ".join(
                loc.get("name", "") for loc in (row.get("locations") or []) if isinstance(loc, dict)
            )
            jobs.append(
                Job(
                    title=title,
                    company=employer.get("company_name") or "Unknown",
                    location=loc,
                    url="https://www.instahyre.com" + (row.get("public_url") or ""),
                    description=truncate(desc),
                    remote=detect_remote(loc, title, desc[:1500]),
                    **parse_salary(
                        f"{row.get('min_salary', '')} {row.get('max_salary', '')} LPA"
                        if row.get("min_salary")
                        else ""
                    ),
                )
            )
        return jobs
