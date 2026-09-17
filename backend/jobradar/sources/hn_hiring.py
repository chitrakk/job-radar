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
SEP = re.compile(r"\s*[|•—–]\s*|\s{2,}")


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

            first_line = text.split("\n", 1)[0].strip()
            parts = [p.strip() for p in SEP.split(first_line) if p.strip()]
            if not parts:
                continue

            company = parts[0][:80]
            # The role is usually the next field that is not a location or a work-mode word.
            title = ""
            for p in parts[1:]:
                if re.search(r"\b(remote|onsite|hybrid|full[- ]time|contract)\b", p, re.I):
                    continue
                title = p[:120]
                break
            if not title:
                title = "See posting"

            posted = None
            if comment.get("created_at_i"):
                posted = datetime.fromtimestamp(comment["created_at_i"], tz=UTC)

            out.append(
                Job(
                    title=title,
                    company=company,
                    location=" ".join(parts[2:4])[:80] if len(parts) > 2 else "",
                    url=f"https://news.ycombinator.com/item?id={comment['id']}",
                    description=truncate(text, 4000),
                    posted_at=posted,
                    remote=detect_remote(first_line, text[:600]),
                    tags=["hacker-news"],
                    **parse_salary(first_line),
                )
            )
        return out
