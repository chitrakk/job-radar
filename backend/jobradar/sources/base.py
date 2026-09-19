"""Source contract and registry.

A source is anything that can turn a Query into a list of Jobs. Adapters register
themselves with @register, so adding a board means adding one file and importing it in
sources/__init__.py — nothing else in the codebase changes.
"""

from __future__ import annotations

import abc
import logging
import time
from datetime import UTC, datetime

import httpx

from ..fetcher import Blocked
from ..models import Job, Query, SourceHealth, Tier

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[Source]] = {}


class Source(abc.ABC):
    """Base class for every job source.

    Subclasses implement `search`; `run` wraps it with timing, health capture and the
    failure policy (Tier 2 never fails a run).
    """

    name: str = ""
    tier: Tier = Tier.TIER1
    # Seconds between requests to this source's host.
    min_interval: float = 1.0
    # Set False in config/sources.yml to skip without deleting the adapter.
    enabled: bool = True
    # True when the source ignores the query and returns a whole board or feed. The pipeline
    # fetches these once per run instead of once per saved query, then filters locally —
    # with five saved queries that is an 80% cut in requests to boards doing us a favour by
    # publishing an open endpoint at all.
    query_independent: bool = False

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}

    # True when this source's list endpoint returns titles without descriptions, so the
    # pipeline should call `hydrate` on the postings it keeps.
    needs_hydration: bool = False

    @abc.abstractmethod
    async def search(self, client: httpx.AsyncClient, query: Query) -> list[Job]:
        """Return postings matching `query`. May raise; `run` handles it."""

    async def hydrate(self, client: httpx.AsyncClient, job: Job) -> bool:
        """Fill in a posting's description from its own page. Return True if it changed.

        Most sources return the description in the list response and need none of this.
        Those that do not — LinkedIn's guest search is titles only — leave every posting
        with an empty body, which silently disables far more than the Details panel:
        skills extraction, skill search, interview prep and CV keyword alignment all read
        the description. On the live corpus that was 242 of 557 jobs, and essentially
        every India-based one.
        """
        return False

    async def run(self, client: httpx.AsyncClient, query: Query) -> tuple[list[Job], SourceHealth]:
        started = time.monotonic()
        try:
            jobs = await self.search(client, query)
        except Blocked as exc:
            # The expected Tier 2 outcome from a datacenter IP. Recorded plainly so the
            # UI can show the source as blocked rather than pretending it returned nothing.
            return [], self._health(False, 0, f"blocked: {exc}", started)
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
            log.warning("source %s failed: %s", self.name, exc)
            return [], self._health(False, 0, f"{type(exc).__name__}: {exc}", started)

        now = datetime.now(UTC)
        for job in jobs:
            job.source = self.name
            job.source_tier = self.tier
            job.last_seen_at = now
        return jobs, self._health(True, len(jobs), "", started)

    def _health(self, ok: bool, n: int, error: str, started: float) -> SourceHealth:
        return SourceHealth(
            source=self.name,
            tier=self.tier,
            ok=ok,
            jobs_found=n,
            error=error[:300],
            duration_ms=int((time.monotonic() - started) * 1000),
        )


def register(cls: type[Source]) -> type[Source]:
    if not cls.name:
        raise ValueError(f"{cls.__name__} must set a name")
    if cls.name in _REGISTRY:
        raise ValueError(f"duplicate source name: {cls.name}")
    _REGISTRY[cls.name] = cls
    return cls


def registry() -> dict[str, type[Source]]:
    return dict(_REGISTRY)


def build_sources(config: dict | None = None) -> list[Source]:
    """Instantiate every enabled source, applying per-source overrides from sources.yml."""
    config = config or {}
    overrides: dict = config.get("sources", {}) or {}
    out: list[Source] = []
    for name, cls in _REGISTRY.items():
        opts = overrides.get(name, {}) or {}
        if not opts.get("enabled", cls.enabled):
            continue
        src = cls(opts)
        if "min_interval" in opts:
            src.min_interval = float(opts["min_interval"])
        out.append(src)
    return out
