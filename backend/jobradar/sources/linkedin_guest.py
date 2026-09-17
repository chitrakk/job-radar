"""LinkedIn public guest job search.

Uses the same unauthenticated endpoint LinkedIn's own logged-out job pages call. No
cookies, no account, no credentials — so there is no account to get restricted, and we
only ever see what any anonymous visitor sees.

LinkedIn does rate-limit datacenter IPs, so this sits at Tier 1 but with the most
conservative interval of any source and a hard page cap. If it starts returning 429s from
Actions it degrades to zero results and shows as unhealthy rather than retrying hard.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from ..fetcher import Blocked, fetch_text
from ..models import Job, Query, Tier
from ..textutil import detect_remote
from .base import Source, register

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE_SIZE = 25


def _clean_url(href: str) -> str:
    """Strip LinkedIn's tracking query string so the same posting dedupes across runs."""
    if not href:
        return ""
    parts = urlsplit(href)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _relative_date(text: str) -> datetime | None:
    """LinkedIn shows '3 days ago' / '2 weeks ago' rather than a timestamp."""
    m = re.search(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", text, re.I)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {
        "minute": timedelta(minutes=n),
        "hour": timedelta(hours=n),
        "day": timedelta(days=n),
        "week": timedelta(weeks=n),
        "month": timedelta(days=30 * n),
    }[unit]
    return datetime.now(UTC) - delta


@register
class LinkedInGuestSource(Source):
    name = "linkedin"
    tier = Tier.TIER1
    # Deliberately slow. This is the source most likely to start 429ing, and backing off
    # here is cheaper than losing it entirely.
    min_interval = 4.0

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        max_pages = int(self.config.get("max_pages", 3))
        location = query.location or ("India" if query.country == "in" else "")
        jobs: list[Job] = []

        for keyword in query.keywords or [""]:
            for page in range(max_pages):
                params = {
                    "keywords": keyword,
                    "location": location,
                    "start": page * PAGE_SIZE,
                    # f_TPR=r<seconds> limits to recently posted.
                    "f_TPR": f"r{query.max_age_days * 86400}",
                }
                if query.remote_only:
                    params["f_WT"] = "2"  # LinkedIn's remote work-type filter

                url = f"{SEARCH}?{urlencode(params)}"
                try:
                    html = await fetch_text(
                        client,
                        url,
                        min_interval=self.min_interval,
                        retries=1,
                        headers={"Accept": "text/html"},
                    )
                except Blocked:
                    raise
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 429:
                        raise Blocked("429 rate limited by linkedin") from exc
                    break

                batch = self._parse(html)
                jobs.extend(batch)
                if len(batch) < PAGE_SIZE:
                    break
        return jobs

    def _parse(self, html: str) -> list[Job]:
        tree = HTMLParser(html)
        out = []
        for card in tree.css("li"):
            title_el = card.css_first("h3")
            company_el = card.css_first("h4 a, .base-search-card__subtitle")
            link_el = card.css_first("a.base-card__full-link, a[href*='/jobs/view/']")
            loc_el = card.css_first(".job-search-card__location")
            date_el = card.css_first("time")

            if not (title_el and link_el):
                continue
            url = _clean_url(link_el.attributes.get("href", ""))
            if not url:
                continue

            posted = None
            if date_el:
                stamp = date_el.attributes.get("datetime")
                if stamp:
                    try:
                        posted = datetime.fromisoformat(stamp).replace(tzinfo=UTC)
                    except ValueError:
                        pass
                posted = posted or _relative_date(date_el.text())

            loc = loc_el.text(strip=True) if loc_el else ""
            title = title_el.text(strip=True)
            out.append(
                Job(
                    title=title,
                    company=company_el.text(strip=True) if company_el else "Unknown",
                    location=loc,
                    url=url,
                    posted_at=posted,
                    remote=detect_remote(loc, title),
                )
            )
        return out
