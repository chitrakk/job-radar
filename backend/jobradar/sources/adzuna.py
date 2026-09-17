"""Adzuna — the one official, licensed aggregator in the mix.

Adzuna indexes Naukri, Indeed and hundreds of Indian boards and resells that legally via
an API, which is why it matters here: it gives us aggregator-grade India coverage without
scraping anything that blocks us. Free tier is ~1,000 calls/month, so we spend calls
deliberately — one call per (keyword, location) pair per run, not per company.

Needs ADZUNA_APP_ID and ADZUNA_APP_KEY (free signup at developer.adzuna.com).
Without them the source disables itself rather than failing the run.
"""

from __future__ import annotations

import os
from urllib.parse import urlencode

import httpx
from dateutil import parser as dateparser

from ..fetcher import fetch_json
from ..models import Job, Query, Tier
from ..textutil import detect_remote, truncate
from .base import Source, register

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"


@register
class AdzunaSource(Source):
    name = "adzuna"
    tier = Tier.TIER1
    min_interval = 1.5

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        app_id = os.getenv("ADZUNA_APP_ID", "")
        app_key = os.getenv("ADZUNA_APP_KEY", "")
        if not (app_id and app_key):
            return []

        per_page = min(50, query.limit)
        pages = self.config.get("pages", 2)
        jobs: list[Job] = []

        for keyword in query.keywords or [""]:
            for page in range(1, pages + 1):
                params = {
                    "app_id": app_id,
                    "app_key": app_key,
                    "results_per_page": per_page,
                    "what": keyword,
                    "max_days_old": query.max_age_days,
                    "content-type": "application/json",
                }
                if query.location:
                    params["where"] = query.location
                url = API.format(country=query.country or "in", page=page) + "?" + urlencode(params)

                data = await fetch_json(client, url, min_interval=self.min_interval, retries=2)
                results = data.get("results", [])
                jobs.extend(self._parse(results))
                if len(results) < per_page:
                    break  # last page; stop spending quota
        return jobs

    def _parse(self, results: list) -> list[Job]:
        out = []
        for r in results:
            loc = (r.get("location") or {}).get("display_name", "")
            desc = r.get("description", "")
            company = (r.get("company") or {}).get("display_name", "")
            lo, hi = r.get("salary_min"), r.get("salary_max")
            posted = None
            if r.get("created"):
                try:
                    posted = dateparser.parse(r["created"])
                except (ValueError, TypeError):
                    pass
            out.append(
                Job(
                    title=r.get("title", ""),
                    company=company or "Unknown",
                    location=loc,
                    url=r.get("redirect_url", ""),
                    description=truncate(desc),
                    posted_at=posted,
                    remote=detect_remote(loc, r.get("title", ""), desc),
                    # Adzuna already returns annualised numbers in local currency.
                    salary_min=lo,
                    salary_max=hi,
                    salary_currency="INR" if (r.get("__country") or "in") == "in" else "",
                    salary_period="year" if lo else "",
                )
            )
        return out
