"""Public ATS job boards — Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee.

This is the backbone of the corpus. Every endpoint here is public, unauthenticated, meant
to be consumed by the company's own careers page, and does not block datacenter IPs — so
unlike the Tier 2 scrapers these keep working from a GitHub Actions runner indefinitely.

The trade-off is that ATS boards have no search: they return a company's entire open
req list. So we poll the companies in config/companies.yml and filter locally. Coverage
therefore scales with that seed list, which phase 3's discovery job grows automatically.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
from dateutil import parser as dateparser

from ..fetcher import Blocked, fetch_json
from ..models import Job, Query, Tier
from ..textutil import detect_remote, html_to_text, parse_salary, truncate
from .base import Source, register


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, int | float):
            # Lever uses epoch milliseconds.
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=UTC)
        return dateparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None


# --------------------------------------------------------------------------- normalisers
# Each returns a list of Jobs for one company token. Shapes differ enough that a
# per-provider function is clearer than a config-driven field mapper.


def _greenhouse(token: str, company: str, data: Any) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        desc = html_to_text(j.get("content", ""))
        loc = (j.get("location") or {}).get("name", "")
        out.append(
            Job(
                title=j.get("title", ""),
                company=company,
                location=loc,
                url=j.get("absolute_url", ""),
                description=truncate(desc),
                posted_at=_dt(j.get("first_published") or j.get("updated_at")),
                remote=detect_remote(loc, j.get("title", ""), desc[:1500]),
                **parse_salary(desc),
            )
        )
    return out


def _lever(token: str, company: str, data: Any) -> list[Job]:
    out = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = cats.get("location", "")
        desc = j.get("descriptionPlain") or html_to_text(j.get("description", ""))
        for section in j.get("lists", []) or []:
            desc += (
                "\n\n" + section.get("text", "") + "\n" + html_to_text(section.get("content", ""))
            )
        out.append(
            Job(
                title=j.get("text", ""),
                company=company,
                location=loc,
                url=j.get("hostedUrl", ""),
                apply_url=j.get("applyUrl", ""),
                description=truncate(desc),
                posted_at=_dt(j.get("createdAt")),
                remote=detect_remote(loc, cats.get("commitment", ""), desc[:1500]),
                **parse_salary(cats.get("salaryRange") and str(cats["salaryRange"]) or desc),
            )
        )
    return out


def _ashby(token: str, company: str, data: Any) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        desc = html_to_text(j.get("descriptionHtml", "")) or j.get("descriptionPlain", "")
        loc = j.get("location", "")
        comp = j.get("compensation") or {}
        salary_src = str(comp.get("compensationTierSummary") or "") or desc
        out.append(
            Job(
                title=j.get("title", ""),
                company=company,
                location=loc,
                url=j.get("jobUrl", ""),
                apply_url=j.get("applyUrl", ""),
                description=truncate(desc),
                posted_at=_dt(j.get("publishedAt")),
                remote=detect_remote(
                    loc, "remote" if j.get("isRemote") else "", j.get("employmentType", "")
                ),
                **parse_salary(salary_src),
            )
        )
    return out


def _workable(token: str, company: str, data: Any) -> list[Job]:
    out = []
    for j in data.get("jobs", []):
        loc_obj = j.get("location") or {}
        loc = ", ".join(
            x for x in [loc_obj.get("city"), loc_obj.get("region"), loc_obj.get("country")] if x
        )
        desc = html_to_text(j.get("description", "") + " " + j.get("requirements", ""))
        out.append(
            Job(
                title=j.get("title", ""),
                company=company,
                location=loc,
                country=loc_obj.get("country", ""),
                url=j.get("url", "") or j.get("application_url", ""),
                description=truncate(desc),
                posted_at=_dt(j.get("published_on") or j.get("created_at")),
                remote=detect_remote(
                    loc, "remote" if loc_obj.get("telecommuting") else "", desc[:1500]
                ),
                **parse_salary(desc),
            )
        )
    return out


def _smartrecruiters(token: str, company: str, data: Any) -> list[Job]:
    out = []
    for j in data.get("content", []):
        loc_obj = j.get("location") or {}
        loc = ", ".join(x for x in [loc_obj.get("city"), loc_obj.get("country")] if x)
        # The list endpoint omits descriptions; a detail call per posting would multiply
        # request volume by ~50x, so we let the enrich step work from the title instead.
        out.append(
            Job(
                title=j.get("name", ""),
                company=company,
                location=loc,
                country=loc_obj.get("country", ""),
                url=f"https://jobs.smartrecruiters.com/{token}/{j.get('id', '')}",
                posted_at=_dt(j.get("releasedDate")),
                remote=detect_remote(loc, "remote" if loc_obj.get("remote") else ""),
            )
        )
    return out


def _recruitee(token: str, company: str, data: Any) -> list[Job]:
    out = []
    for j in data.get("offers", []):
        loc = j.get("location", "") or ", ".join(
            x for x in [j.get("city"), j.get("country_code")] if x
        )
        desc = html_to_text((j.get("description") or "") + " " + (j.get("requirements") or ""))
        out.append(
            Job(
                title=j.get("title", ""),
                company=company,
                location=loc,
                url=j.get("careers_url", "") or j.get("url", ""),
                description=truncate(desc),
                posted_at=_dt(j.get("published_at")),
                remote=detect_remote(loc, j.get("remote") and "remote" or "", desc[:1500]),
                **parse_salary(desc),
            )
        )
    return out


PROVIDERS: dict[str, dict[str, Any]] = {
    "greenhouse": {
        "url": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
        "parse": _greenhouse,
    },
    "lever": {
        "url": "https://api.lever.co/v0/postings/{token}?mode=json",
        "parse": _lever,
    },
    "ashby": {
        "url": "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
        "parse": _ashby,
    },
    "workable": {
        "url": "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
        "parse": _workable,
    },
    "smartrecruiters": {
        "url": "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100",
        "parse": _smartrecruiters,
    },
    "recruitee": {
        "url": "https://{token}.recruitee.com/api/offers/",
        "parse": _recruitee,
    },
}


class ATSSource(Source):
    """Polls every configured company board for one ATS provider."""

    provider: str = ""
    tier = Tier.TIER1
    query_independent = True
    min_interval = 0.35
    # Boards are independent hosts for Recruitee and shared for the rest; cap concurrency
    # so a 300-company seed list does not open 300 sockets at once.
    concurrency = 8

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        companies: list[Any] = self.config.get("companies") or []
        if not companies:
            return []

        spec = PROVIDERS[self.provider]
        sem = asyncio.Semaphore(self.concurrency)

        async def one(entry: Any) -> list[Job]:
            # Entries are either "token" or {token: ..., name: ...}.
            if isinstance(entry, dict):
                token, name = entry.get("token", ""), entry.get("name", "")
            else:
                token, name = str(entry), ""
            if not token:
                return []
            display = name or token.replace("-", " ").replace("_", " ").title()
            url = spec["url"].format(token=token)
            async with sem:
                try:
                    data = await fetch_json(client, url, min_interval=self.min_interval, retries=2)
                except Blocked:
                    raise
                except Exception:
                    # A single dead board token (company churned ATS, board went private)
                    # is normal and must not take down the other 200.
                    return []
            try:
                return spec["parse"](token, display, data)
            except Exception:
                return []

        results = await asyncio.gather(*(one(c) for c in companies), return_exceptions=True)

        jobs: list[Job] = []
        blocked = 0
        for r in results:
            if isinstance(r, list):
                jobs.extend(r)
            elif isinstance(r, Blocked):
                blocked += 1

        # gather(return_exceptions=True) turns a Blocked into a value, so without this the
        # provider would report healthy-with-zero-jobs — the exact silent degradation the
        # health reporting exists to prevent. Only escalate when nothing came back at all;
        # one blocked board among many is just a dead token.
        if blocked and not jobs:
            raise Blocked(f"all {blocked} {self.provider} boards refused the request")
        return jobs


def _make(provider: str) -> type[ATSSource]:
    cls = type(
        f"{provider.title()}Source",
        (ATSSource,),
        {"name": f"ats:{provider}", "provider": provider},
    )
    return register(cls)  # type: ignore[arg-type]


GreenhouseSource = _make("greenhouse")
LeverSource = _make("lever")
AshbySource = _make("ashby")
WorkableSource = _make("workable")
SmartRecruitersSource = _make("smartrecruiters")
RecruiteeSource = _make("recruitee")
