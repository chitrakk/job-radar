"""Remote job boards with free public feeds.

These matter for an India-based search because remote roles are the main route to
international pay without relocating, and several of these boards explicitly list roles
open to India / Asia timezones. All are free, unauthenticated JSON or RSS, and none block
datacenter IPs — the boards publish these feeds precisely so aggregators consume them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree

import httpx
from dateutil import parser as dateparser

from ..fetcher import fetch_json, fetch_text
from ..models import Job, Query, RemoteKind, Tier
from ..textutil import html_to_text, parse_salary, truncate
from .base import Source, register


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        if isinstance(v, int | float):
            return datetime.fromtimestamp(float(v), tz=UTC)
        return dateparser.parse(str(v))
    except (ValueError, TypeError, OverflowError):
        return None


class RemoteFeed(Source):
    """Shared behaviour: fetch one feed, normalise, mark everything remote."""

    tier = Tier.TIER1
    min_interval = 1.0
    url = ""
    query_independent = True

    def _finish(self, jobs: list[Job]) -> list[Job]:
        for j in jobs:
            if j.remote == RemoteKind.UNKNOWN:
                j.remote = RemoteKind.REMOTE
        return jobs


@register
class RemoteOKSource(RemoteFeed):
    name = "remoteok"
    url = "https://remoteok.com/api"

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        data = await fetch_json(client, self.url, min_interval=self.min_interval, retries=2)
        out = []
        # The first element is a legal/attribution notice, not a job.
        for j in data[1:] if isinstance(data, list) else []:
            if not j.get("position"):
                continue
            desc = html_to_text(j.get("description", ""))
            salary = {}
            if j.get("salary_min"):
                salary = {
                    "salary_min": float(j["salary_min"]),
                    "salary_max": float(j.get("salary_max") or j["salary_min"]),
                    "salary_currency": "USD",
                    "salary_period": "year",
                }
            out.append(
                Job(
                    title=j.get("position", ""),
                    company=j.get("company", "Unknown"),
                    location=j.get("location", "") or "Remote",
                    url=j.get("url", "") or j.get("apply_url", ""),
                    description=truncate(desc),
                    posted_at=_dt(j.get("epoch") or j.get("date")),
                    tags=[t for t in (j.get("tags") or []) if isinstance(t, str)][:12],
                    **salary,
                )
            )
        return self._finish(out)


@register
class RemotiveSource(RemoteFeed):
    name = "remotive"
    query_independent = False  # supports server-side ?search=
    url = "https://remotive.com/api/remote-jobs"

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        jobs: list[Job] = []
        seen: set[str] = set()
        for keyword in query.keywords or [""]:
            url = f"{self.url}?limit=100" + (f"&search={keyword}" if keyword else "")
            data = await fetch_json(client, url, min_interval=self.min_interval, retries=2)
            for j in data.get("jobs", []):
                if j.get("url") in seen:
                    continue
                seen.add(j.get("url", ""))
                desc = html_to_text(j.get("description", ""))
                jobs.append(
                    Job(
                        title=j.get("title", ""),
                        company=j.get("company_name", "Unknown"),
                        location=j.get("candidate_required_location", "") or "Remote",
                        url=j.get("url", ""),
                        description=truncate(desc),
                        posted_at=_dt(j.get("publication_date")),
                        tags=[t for t in (j.get("tags") or []) if isinstance(t, str)][:12],
                        **parse_salary(j.get("salary", "") or ""),
                    )
                )
        return self._finish(jobs)


@register
class ArbeitnowSource(RemoteFeed):
    name = "arbeitnow"
    url = "https://www.arbeitnow.com/api/job-board-api"

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        out = []
        pages = int(self.config.get("pages", 2))
        for page in range(1, pages + 1):
            data = await fetch_json(
                client, f"{self.url}?page={page}", min_interval=self.min_interval, retries=2
            )
            rows = data.get("data", [])
            if not rows:
                break
            for j in rows:
                desc = html_to_text(j.get("description", ""))
                out.append(
                    Job(
                        title=j.get("title", ""),
                        company=j.get("company_name", "Unknown"),
                        location=j.get("location", ""),
                        url=j.get("url", ""),
                        description=truncate(desc),
                        posted_at=_dt(j.get("created_at")),
                        remote=RemoteKind.REMOTE if j.get("remote") else RemoteKind.UNKNOWN,
                        tags=[t for t in (j.get("tags") or []) if isinstance(t, str)][:12],
                        **parse_salary(desc),
                    )
                )
        # Arbeitnow carries non-remote German roles too, so do not blanket-mark remote.
        return out


@register
class HimalayasSource(RemoteFeed):
    name = "himalayas"
    url = "https://himalayas.app/jobs/api"

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        data = await fetch_json(
            client, f"{self.url}?limit=100", min_interval=self.min_interval, retries=2
        )
        out = []
        for j in data.get("jobs", []):
            desc = html_to_text(j.get("description", ""))
            locations = j.get("locationRestrictions") or []
            salary = {}
            if j.get("minSalary"):
                salary = {
                    "salary_min": float(j["minSalary"]),
                    "salary_max": float(j.get("maxSalary") or j["minSalary"]),
                    "salary_currency": j.get("currency", "USD") or "USD",
                    "salary_period": "year",
                }
            out.append(
                Job(
                    title=j.get("title", ""),
                    company=j.get("companyName", "Unknown"),
                    location=", ".join(locations) if locations else "Remote",
                    url=j.get("applicationLink", "") or j.get("guid", ""),
                    description=truncate(desc),
                    posted_at=_dt(j.get("pubDate")),
                    **salary,
                )
            )
        return self._finish(out)


@register
class WeWorkRemotelySource(RemoteFeed):
    name = "weworkremotely"
    url = "https://weworkremotely.com/remote-jobs.rss"

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        xml = await fetch_text(client, self.url, min_interval=self.min_interval, retries=2)
        out = []
        # Parsed as XML, not via selectolax: an HTML parser treats <link> as a void element
        # and drops its text, so every job URL comes back empty and the whole feed yields
        # nothing. ElementTree handles the RSS namespace-free structure correctly.
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError:
            return []

        for item in root.iter("item"):
            raw_title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not raw_title or not link:
                continue
            # WWR encodes "Company: Role" in the RSS title.
            company, _, role = raw_title.partition(":")
            if not role:
                company, role = "Unknown", raw_title
            desc = html_to_text(item.findtext("description") or "")
            region = (item.findtext("region") or "").strip()
            out.append(
                Job(
                    title=role.strip(),
                    company=company.strip() or "Unknown",
                    location=region or "Remote",
                    url=link,
                    description=truncate(desc),
                    posted_at=_dt(item.findtext("pubDate")),
                    **parse_salary(desc),
                )
            )
        return self._finish(out)
