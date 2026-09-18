"""Shared HTTP client for Tier 1 sources.

Tier 2 scraping goes through sources/stealth.py (Scrapling) instead, but both honour the
same rate limits and the same JOBRADAR_PROXY_URL, so adding a residential proxy later
turns on Tier 2 without touching any adapter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)

# Boards serve different markup to obviously-automated clients. A plain honest browser UA
# is enough for every Tier 1 source; Tier 2 needs Scrapling's full fingerprinting.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
RETRY_STATUS = {429, 500, 502, 503, 504}


class RateLimiter:
    """Per-host minimum interval between requests.

    Sources are polled concurrently, so without this we would hit e.g. every Greenhouse
    board at once and look exactly like the traffic these sites block.
    """

    def __init__(self) -> None:
        self._last: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def wait(self, url: str, min_interval: float) -> None:
        # Checked per call so tests can switch politeness delays off. Never set this in
        # production — the delays are what keep us welcome on these endpoints.
        if os.getenv("JOBRADAR_NO_THROTTLE"):
            return
        host = urlsplit(url).netloc
        async with self._locks[host]:
            delta = time.monotonic() - self._last[host]
            if delta < min_interval:
                await asyncio.sleep(min_interval - delta)
            self._last[host] = time.monotonic()


_limiter = RateLimiter()


class Blocked(Exception):
    """Raised when a source actively refuses us (403/406/451 or a challenge page).

    Distinct from a transport error because it is not worth retrying — it means the IP
    or the fingerprint is the problem, which is the expected Tier 2 outcome on cloud runners.
    """


def proxy_url() -> str | None:
    return os.getenv("JOBRADAR_PROXY_URL") or None


def make_client(**kw: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-IN,en;q=0.9",
        },
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        proxy=proxy_url(),
        **kw,
    )


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str = "GET",
    min_interval: float = 1.0,
    retries: int = 3,
    **kw: Any,
) -> httpx.Response:
    """One HTTP request with rate limiting, retry and backoff.

    Raises Blocked on a hard refusal, httpx.HTTPError once retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        await _limiter.wait(url, min_interval)
        try:
            resp = await client.request(method, url, **kw)
        except httpx.HTTPError as exc:
            last_exc = exc
            log.debug("transport error %s (attempt %d): %s", url, attempt + 1, exc)
        else:
            if resp.status_code in (403, 406, 451):
                # Naukri answers datacenter IPs with 406; Cloudflare uses 403.
                raise Blocked(f"{resp.status_code} from {urlsplit(url).netloc}")
            if resp.status_code not in RETRY_STATUS:
                resp.raise_for_status()
                return resp
            last_exc = httpx.HTTPStatusError(
                f"{resp.status_code}", request=resp.request, response=resp
            )
            log.debug("retryable %s from %s (attempt %d)", resp.status_code, url, attempt + 1)

        if attempt < retries:
            # Jittered exponential backoff; jitter matters because all sources start together.
            await asyncio.sleep((2**attempt) + random.uniform(0, 0.5))

    assert last_exc is not None
    raise last_exc


async def fetch_json(client: httpx.AsyncClient, url: str, **kw: Any) -> Any:
    resp = await fetch(client, url, **kw)
    return resp.json()


async def fetch_text(client: httpx.AsyncClient, url: str, **kw: Any) -> str:
    resp = await fetch(client, url, **kw)
    return resp.text
