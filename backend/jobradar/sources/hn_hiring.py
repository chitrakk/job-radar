"""Hacker News "Ask HN: Who is hiring?" via the public Algolia HN API.

Worth its own adapter because this thread is where a lot of remote-friendly startups post
roles that never reach a job board, including India-remote ones. Each top-level comment is
one posting, written freeform, so parsing is heuristic — we extract what we can and let
the phase 2 LLM enrichment clean up the rest.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import httpx

from ..fetcher import fetch_json
from ..models import Job, Query, Tier
from ..textutil import detect_remote, html_to_text, parse_salary, truncate
from .base import Source, register

SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
ITEMS = "https://hn.algolia.com/api/v1/items/{id}"

# "Company | Role | Location | Remote | Salary" is the thread's conventional first line.
# Only the pipe and bullet are true field separators. An en/em dash is NOT: it appears
# inside salary ranges ("€75k–110k") and hyphenated place names, and splitting on it
# fragments the very fields we are trying to read.
SEP = re.compile(r"\s*[|•]\s*")

# Field values that describe how or when you work, never what the role is.
_WORK_MODE = re.compile(
    r"^\s*(?:100%\s*)?(?:fully\s*)?(?:remote|onsite|on-site|hybrid|wfh|in[- ]office"
    r"|full[- ]?time|part[- ]?time|contract|permanent|freelance|intern(?:ship)?|visa"
    r"|h1b|relocation|equity|competitive)\b",
    re.I,
)

# A field that is a place rather than a role.
_LOOKS_LIKE_LOCATION = re.compile(
    r"\b(remote|onsite|on-site|hybrid|anywhere|worldwide|usa|us|uk|eu|europe|india|canada"
    r"|london|berlin|paris|amsterdam|nyc|new york|san francisco|sf|bay area|seattle|austin"
    r"|boston|chicago|toronto|bengaluru|bangalore|mumbai|delhi|hyderabad|pune|singapore"
    r"|tokyo|sydney|dublin|zurich|tel aviv|utrecht|barcelona|madrid|lisbon)\b",
    re.I,
)

_URL = re.compile(r"https?://\S+|www\.\S+")
_SALARY_FIELD = re.compile(r"[€$£₹]\s*\d|\b\d+\s*k\b|\bLPA\b|\bsalary\b", re.I)

# Comments that are discussion, not postings.
_NOT_A_POSTING = re.compile(
    r"^\s*(?:please\s+normalize|reminder|meta\b|ps[:.]|off[- ]topic|does anyone|"
    r"anyone (?:else )?(?:know|hiring)|why (?:do|are|is)|how (?:do|come)|"
    r"i'?m looking for (?:a|work)|seeking work|wanted:)",
    re.I,
)

# A role field almost always contains one of these. Used as a positive signal when the
# field order is unusual, not as a hard filter.
_ROLE_WORDS = re.compile(
    r"\b(engineer|developer|scientist|analyst|manager|designer|architect|lead|director"
    r"|founding|head|researcher|consultant|specialist|administrator|devops|sre|swe|sde"
    r"|programmer|marketer|writer|recruiter|intern|cto|cio|vp|pm|product|data|ml|ai"
    r"|frontend|front[- ]end|backend|back[- ]end|fullstack|full[- ]stack|mobile|ios"
    r"|android|qa|security|platform|infrastructure|support|sales|ops|operations)\b",
    re.I,
)


def parse_posting(text: str) -> tuple[str, str, str] | None:
    """Pull (company, title, location) out of one HN hiring comment.

    Returns None when the comment is not a job posting at all.

    Worth the care: these comments are freeform, and a sloppy parse does not merely lose a
    job — it produces a record with a useless title and an empty location, which then
    passes location filters that a correctly-parsed foreign job would fail.
    """
    first_line = text.split("\n", 1)[0].strip()
    if not first_line or _NOT_A_POSTING.search(first_line):
        return None

    # Drop URLs before splitting; they carry no field information and often contain
    # characters that confuse the field order.
    cleaned = _URL.sub(" ", first_line).strip(" |•-")
    parts = [p.strip(" -–—") for p in SEP.split(cleaned) if p.strip(" -–—")]

    if len(parts) >= 2:
        company = parts[0][:80]
        title = ""
        location_bits: list[str] = []

        for field in parts[1:]:
            if not title and not _WORK_MODE.match(field) and not _SALARY_FIELD.search(field):
                # The first field that is neither a work mode nor money is the role —
                # unless it is plainly a location and something later looks like a role.
                if _LOOKS_LIKE_LOCATION.search(field) and not _ROLE_WORDS.search(field):
                    location_bits.append(field)
                    continue
                title = field[:140]
                continue
            if _LOOKS_LIKE_LOCATION.search(field) or _WORK_MODE.match(field):
                location_bits.append(field)

        if title:
            return company, title, " · ".join(location_bits)[:90]

        # Pipes present but no field reads as a role: often "Company | City | Full-time"
        # with the roles listed further down the comment.
        body_title = _title_from_body(text)
        if body_title:
            return company, body_title, " · ".join(location_bits)[:90]
        return None

    # No pipes. Try "<Role> at <Company> in <Place>", the other common shape.
    m = re.match(
        r"^(?P<title>[^,.]{4,90}?)\s+(?:at|@|with)\s+(?P<company>[^,.|]{2,60})"
        r"(?:\s+in\s+(?P<loc>[^,.|]{2,50}))?",
        first_line,
    )
    if m and _ROLE_WORDS.search(m.group("title")):
        return (
            m.group("company").strip()[:80],
            m.group("title").strip()[:140],
            (m.group("loc") or "").strip()[:90],
        )

    return None


def _title_from_body(text: str) -> str:
    """Find a role named in the body when the header did not carry one."""
    for line in text.split("\n")[1:12]:
        candidate = line.strip(" -–—•*\t")
        if 4 <= len(candidate) <= 90 and _ROLE_WORDS.search(candidate):
            # Skip prose: a role line is short and rarely ends in a full stop.
            if candidate.endswith(".") or len(candidate.split()) > 10:
                continue
            return candidate[:140]
    return ""


@register
class HackerNewsHiringSource(Source):
    name = "hn_hiring"
    tier = Tier.TIER1
    query_independent = True
    min_interval = 1.0

    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        threads = await fetch_json(
            client,
            f"{SEARCH}?query=who+is+hiring&tags=story,author_whoishiring&hitsPerPage=2",
            min_interval=self.min_interval,
            retries=2,
        )
        hits = threads.get("hits", [])
        if not hits:
            return []

        jobs: list[Job] = []
        # Only the most recent one or two months; older threads are stale postings.
        for hit in hits[:2]:
            thread = await fetch_json(
                client,
                ITEMS.format(id=hit["objectID"]),
                min_interval=self.min_interval,
                retries=2,
            )
            jobs.extend(self._parse_thread(thread))
        return jobs

    def _parse_thread(self, thread: dict) -> list[Job]:
        out = []
        for comment in thread.get("children", []):
            if not comment.get("text") or comment.get("author") == "whoishiring":
                continue
            text = html_to_text(comment["text"])
            if len(text) < 60:
                continue

            parsed = parse_posting(text)
            if parsed is None:
                # Not a job posting — the thread also carries meta-commentary and replies.
                continue
            company, title, location = parsed

            posted = None
            if comment.get("created_at_i"):
                posted = datetime.fromtimestamp(comment["created_at_i"], tz=UTC)

            first_line = text.split("\n", 1)[0].strip()
            out.append(
                Job(
                    title=title,
                    company=company,
                    location=location,
                    url=f"https://news.ycombinator.com/item?id={comment['id']}",
                    description=truncate(text, 4000),
                    posted_at=posted,
                    remote=detect_remote(first_line, text[:600]),
                    tags=["hacker-news"],
                    **parse_salary(first_line),
                )
            )
        return out
