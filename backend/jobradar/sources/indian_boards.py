"""Indian job boards that answer a datacenter IP: Shine, Internshala and Wellfound.

Why these three, and why no browser. Measured from a GitHub Actions runner with
scripts/probe_blocks.mjs (see .github/workflows/probe.yml):

  - Naukri and Foundit sit behind Akamai. Naukri's API answers
    `{"message":"recaptcha required","statusCode":406}` and its HTML is a bot-trap shell;
    headless Chromium is refused harder than a plain request. Not reachable without a
    residential proxy and a captcha solver.
  - Indeed India passes a real browser from a home IP but gets Cloudflare's
    "Just a moment..." from Azure. TimesJobs renders empty from Azure.
  - Shine, Internshala and Wellfound return their full listings to a *plain* HTTP request
    from Azure. Shine embeds a structured `jobs` array in its streamed Next.js payload,
    Wellfound ships an Apollo cache in `__NEXT_DATA__`, and Internshala server-renders
    every card. A browser would add a hundred megabytes and a minute per run for nothing.

Every path fetched here is allowed for `User-agent: *` by the site's robots.txt, checked
with urllib.robotparser rather than by eye. Requests are paced per host and each URL is
fetched at most once per run, however many saved queries share a keyword.

These are HTML scrapes, so they are Tier 2: a redesign can break a parser. When that
happens the source reports zero jobs in the health strip rather than failing the run.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from ..fetcher import fetch, fetch_text
from ..models import Job, Query, RemoteKind, Seniority, Tier
from ..textutil import detect_remote, html_to_text, parse_salary, salary_from_text
from .base import Source, register

IST = timezone(timedelta(hours=5, minutes=30))

# Reused by every adapter: a job board's own "this role is in N places" list can run to a
# dozen cities and a few countries. Beyond this many we keep the first few and say so.
MAX_LOCATIONS = 4


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _years_to_seniority(text: str) -> Seniority:
    """'0 to 4 Yrs' / '1 year(s)' / '3-5 years' -> a level, from the *minimum* asked for.

    The minimum is what decides whether a candidate clears the bar, so it is the useful
    number: "2 to 7 Yrs" is a role a mid-level candidate can apply for.
    """
    m = re.search(r"(\d+)", text or "")
    if not m:
        return Seniority.UNKNOWN
    years = int(m.group(1))
    if years < 2:
        return Seniority.ENTRY
    if years < 5:
        return Seniority.MID
    if years < 8:
        return Seniority.SENIOR
    return Seniority.LEAD


def _pay(label: str) -> dict[str, Any]:
    """Salary fields for Job(), keeping the board's own wording as salary_text.

    Returns nothing for "Not disclosed" and anything parse_salary is not confident about —
    a wrong salary is worse than a missing one, because people filter on it.
    """
    if not label or "disclosed" in label.lower():
        return {}
    pay = parse_salary(label)
    if pay:
        pay["salary_text"] = label.strip()[:120]
    return pay


def _join_locations(places: list[str]) -> str:
    places = [p.strip() for p in places if p and p.strip()]
    if len(places) <= MAX_LOCATIONS:
        return ", ".join(places)
    return ", ".join(places[:MAX_LOCATIONS]) + f" +{len(places) - MAX_LOCATIONS} more"


def _relative_date(text: str) -> datetime | None:
    """'Few hours ago', 'Today', '3 days ago', '2 weeks ago', '1 month ago'."""
    t = (text or "").lower()
    now = datetime.now(UTC)
    if re.search(r"just now|few (hours|minutes)|today|hour", t):
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*(day|week|month)", t)
    if not m:
        return None
    n = int(m.group(1))
    return (
        now
        - {
            "day": timedelta(days=n),
            "week": timedelta(weeks=n),
            "month": timedelta(days=30 * n),
        }[m.group(2)]
    )


def jobposting_from_html(html: str) -> dict[str, Any] | None:
    """The first schema.org JobPosting embedded as JSON-LD, if the page has one.

    Detail pages publish this for Google for Jobs, so it is the most stable thing on the
    page — far more so than class names that change with every redesign.
    """
    for raw in re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


class BoardSource(Source):
    """Shared plumbing: pacing, per-run URL de-duplication, city fan-out."""

    tier = Tier.TIER2
    min_interval = 1.5

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(config)
        # One instance serves every saved query in a run, and "data analyst" appears in
        # more than one of them. Fetching the same page twice teaches us nothing.
        self._fetched: set[str] = set()

    @property
    def cities(self) -> list[str]:
        return [str(c) for c in self.config.get("cities", []) or []]

    @property
    def pages(self) -> int:
        return int(self.config.get("pages", 1))

    async def _get(self, client: httpx.AsyncClient, url: str) -> str | None:
        if url in self._fetched:
            return None
        self._fetched.add(url)
        try:
            return await fetch_text(
                client,
                url,
                min_interval=self.min_interval,
                retries=1,
                headers={"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-IN"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise


# ============================================================================== Shine


_NEXT_CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)


def _next_flight_text(html: str) -> str:
    """Reassemble a Next.js app-router flight payload from its streamed chunks.

    Each chunk is a JavaScript string literal, so json.loads on the quoted literal is the
    correct decoder — `unicode_escape` would mangle every ₹ and accented name.
    """
    out = []
    for chunk in _NEXT_CHUNK.findall(html):
        try:
            out.append(json.loads(f'"{chunk}"'))
        except json.JSONDecodeError:
            continue
    return "".join(out)


def _embedded_array(blob: str, key: str) -> list[dict[str, Any]]:
    """Decode the JSON array that follows `"key":` inside a larger non-JSON blob."""
    marker = f'"{key}":['
    start = blob.find(marker)
    if start < 0:
        return []
    try:
        value, _ = json.JSONDecoder().raw_decode(blob, start + len(marker) - 1)
    except json.JSONDecodeError:
        return []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


@register
class ShineSource(BoardSource):
    """shine.com — a large domestic board, strong in Delhi NCR.

    Search pages embed a structured `jobs` array (title, company, locations, postedAt,
    skills, experience band, salary label). Detail pages carry a JSON-LD JobPosting with
    the full description, fetched later by hydrate().
    """

    name = "shine"
    needs_hydration = True
    BASE = "https://www.shine.com"

    def _urls(self, keyword: str) -> list[str]:
        slug = slugify(keyword)
        if not slug:
            return []
        stem = f"{self.BASE}/job-search/{slug}-jobs"
        # India-wide, paginated as -2, -3 (a ?page= parameter is silently ignored).
        urls = [stem] + [f"{stem}-{n}" for n in range(2, self.pages + 1)]
        urls += [f"{stem}-in-{slugify(city)}" for city in self.cities]
        return urls

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        jobs: list[Job] = []
        for keyword in query.keywords:
            for url in self._urls(keyword):
                html = await self._get(client, url)
                if html:
                    jobs.extend(self.parse(html))
        return jobs

    def parse(self, html: str) -> list[Job]:
        out = []
        for row in _embedded_array(_next_flight_text(html), "jobs"):
            title, company, path = row.get("title"), row.get("companyName"), row.get("detailUrl")
            if not (title and company and path):
                continue

            posted = None
            if stamp := row.get("postedAt"):
                try:
                    posted = datetime.fromisoformat(stamp)
                    # Shine's timestamps are naive IST, not UTC.
                    if posted.tzinfo is None:
                        posted = posted.replace(tzinfo=IST)
                except ValueError:
                    pass

            places = [p for p in row.get("locations") or [] if p]
            location = _join_locations(places)
            experience = row.get("experienceLabel") or ""
            salary_label = row.get("salaryLabel") or ""
            pay = _pay(salary_label)

            job = Job(
                title=title,
                company=company,
                location=location if "india" in location.lower() else f"{location}, India",
                url=f"{self.BASE}{path}",
                posted_at=posted,
                skills=[s for s in row.get("skills") or [] if isinstance(s, str)][:12],
                seniority=_years_to_seniority(experience),
                remote=detect_remote(location, title),
                **pay,
            )
            if experience:
                job.tags.append(f"Experience: {experience}")
            out.append(job)
        return out

    async def hydrate(self, client: httpx.AsyncClient, job: Job) -> bool:
        html = await fetch_text(
            client,
            job.url,
            min_interval=self.min_interval,
            retries=0,
            headers={"Accept": "text/html", "Accept-Language": "en-IN"},
        )
        posting = jobposting_from_html(html)
        if not posting:
            return False
        text = html_to_text(str(posting.get("description") or ""))
        if len(text) < 80:
            return False
        job.description = text
        if stamp := posting.get("datePosted"):
            try:
                job.posted_at = datetime.fromisoformat(stamp)
            except ValueError:
                pass
        job.remote = detect_remote(job.location, job.title, text[:2000])
        # salary_from_text, not parse_salary: a description is prose, and running the
        # label parser over its first 300 characters published one posting at ₹20.2 crore
        # because the advert said "CTC: 2021 LPA".
        if job.salary_max is None and (pay := salary_from_text(text)):
            for key, value in pay.items():
                setattr(job, key, value)
        return True


# ======================================================================== Internshala


@register
class InternshalaSource(BoardSource):
    """internshala.com — skews early-career, which is exactly who struggles on the
    senior-heavy global boards. Cards are server-rendered with the description, skills,
    salary and experience inline, so no second request is needed."""

    name = "internshala"
    BASE = "https://internshala.com"
    # robots.txt asks some crawlers for a 1-5s delay; honour the upper end.
    min_interval = 2.0

    def _urls(self, keyword: str) -> list[str]:
        slug = slugify(keyword)
        if not slug:
            return []
        stem = f"{self.BASE}/jobs/keywords-{slug}/"
        urls = [stem] + [f"{stem}page-{n}/" for n in range(2, self.pages + 1)]
        urls += [f"{self.BASE}/jobs/{slug}-jobs-in-{slugify(city)}/" for city in self.cities]
        return urls

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        jobs: list[Job] = []
        for keyword in query.keywords:
            for url in self._urls(keyword):
                html = await self._get(client, url)
                if html:
                    jobs.extend(self.parse(html))
        return jobs

    def parse(self, html: str) -> list[Job]:
        tree = HTMLParser(html)
        out = []
        for card in tree.css("div.individual_internship"):
            # The same listing mixes internships in; this source is for jobs.
            if (card.attributes.get("employment_type") or "job") != "job":
                continue
            link = card.css_first("a.job-title-href")
            company_el = card.css_first(".company-name")
            if not (link and company_el):
                continue
            href = link.attributes.get("href") or card.attributes.get("data-href") or ""
            if not href:
                continue

            places = [a.text(strip=True) for a in card.css(".locations a")]
            location = _join_locations(places)

            salary = ""
            experience = ""
            for item in card.css(".row-1-item"):
                icon = item.css_first("i")
                cls = icon.attributes.get("class", "") if icon else ""
                if "money" in cls:
                    span = item.css_first("span.mobile") or item.css_first("span")
                    salary = span.text(strip=True) if span else ""
                elif "briefcase" in cls:
                    span = item.css_first("span")
                    experience = span.text(strip=True) if span else ""

            posted = None
            for label in card.css(".color-labels span"):
                posted = posted or _relative_date(label.text(strip=True))

            about = card.css_first(".about_job .text")
            description = about.text(strip=True) if about else ""
            title = link.text(strip=True)
            pay = _pay(salary)
            wfh = any("work from home" in p.lower() for p in places)

            job = Job(
                title=title,
                company=company_el.text(strip=True),
                location=(location or "India")
                if "india" in location.lower()
                else f"{location}, India",
                url=f"{self.BASE}{href}" if href.startswith("/") else href,
                description=description,
                posted_at=posted,
                skills=[s.text(strip=True) for s in card.css(".job_skill")][:12],
                seniority=_years_to_seniority(experience),
                remote=RemoteKind.REMOTE
                if wfh
                else detect_remote(location, title, description[:1500]),
                **pay,
            )
            if experience:
                job.tags.append(f"Experience: {experience}")
            out.append(job)
        return out


# ========================================================================== Wellfound


@register
class WellfoundSource(BoardSource):
    """wellfound.com (formerly AngelList Talent) — startups, with salary bands on nearly
    every listing. Role pages are scoped by place: /role/l/<role>/<place>.

    Unknown roles and unknown places do not 404; they redirect to a broader page, and
    "data-scientist/delhi" quietly becomes a worldwide Data Scientist list. So a response
    whose final URL is not the one we asked for is discarded rather than trusted.
    """

    name = "wellfound"
    BASE = "https://wellfound.com"
    min_interval = 2.0

    @property
    def places(self) -> list[str]:
        return [str(p) for p in self.config.get("places", ["india"]) or ["india"]]

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        jobs: list[Job] = []
        for keyword in query.keywords:
            role = slugify(keyword)
            if not role:
                continue
            for place in self.places:
                path = f"/role/l/{role}/{slugify(place)}"
                for page in range(1, self.pages + 1):
                    url = f"{self.BASE}{path}" + (f"?page={page}" if page > 1 else "")
                    if url in self._fetched:
                        continue
                    self._fetched.add(url)
                    try:
                        resp = await fetch(
                            client,
                            url,
                            min_interval=self.min_interval,
                            retries=1,
                            headers={"Accept": "text/html", "Accept-Language": "en-IN"},
                        )
                    except httpx.HTTPStatusError:
                        break
                    if urlsplit(str(resp.url)).path.rstrip("/") != path:
                        break  # redirected to some broader page; not what we asked for
                    batch = self.parse(resp.text)
                    jobs.extend(batch)
                    if not batch:
                        break
        return jobs

    def parse(self, html: str) -> list[Job]:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            return []
        try:
            state = json.loads(m.group(1))["props"]["pageProps"]["apolloState"]["data"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

        # Listings do not name their company; the startup records point at listings.
        company_of: dict[str, str] = {}
        for node in state.values():
            if isinstance(node, dict) and node.get("__typename") == "StartupResult":
                for ref in node.get("highlightedJobListings") or []:
                    key = (ref or {}).get("__ref", "")
                    company_of[key.split(":", 1)[-1]] = node.get("name") or ""

        out = []
        for node in state.values():
            if not (isinstance(node, dict) and node.get("__typename") == "JobListingSearchResult"):
                continue
            jid, title = str(node.get("id") or ""), node.get("title") or ""
            company = company_of.get(jid, "")
            if not (jid and title and company):
                continue

            places = node.get("locationNames") or []
            remote_kind = ((node.get("remoteConfig") or {}).get("kind") or "").upper()
            accepted = node.get("acceptedRemoteLocationNames") or []
            if places:
                location = _join_locations(places)
            elif node.get("remote"):
                location = f"Remote ({', '.join(accepted)})" if accepted else "Remote"
            else:
                location = ""

            posted = None
            if stamp := node.get("liveStartAt"):
                try:
                    posted = datetime.fromtimestamp(int(stamp), tz=UTC)
                except (TypeError, ValueError, OSError):
                    pass

            comp = node.get("compensation") or ""
            pay = _pay(comp.split("•")[0])
            lo, hi = node.get("yearsExperienceMin"), node.get("yearsExperienceMax")

            job = Job(
                title=title,
                company=company,
                location=location,
                url=f"{self.BASE}/jobs/{jid}-{node.get('slug') or slugify(title)}",
                description=html_to_text(node.get("description") or ""),
                posted_at=posted,
                remote=(
                    RemoteKind.REMOTE
                    if remote_kind == "REMOTE" or (node.get("remote") and not places)
                    else RemoteKind.HYBRID
                    if remote_kind == "HYBRID"
                    else detect_remote(location, title)
                ),
                seniority=_years_to_seniority(str(lo)) if lo is not None else Seniority.UNKNOWN,
                **pay,
            )
            if lo is not None:
                job.tags.append(f"Experience: {lo}-{hi} yrs" if hi else f"Experience: {lo}+ yrs")
            if node.get("jobType"):
                job.tags.append(str(node["jobType"]).replace("-", " ").capitalize())
            out.append(job)
        return out
