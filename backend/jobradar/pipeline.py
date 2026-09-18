"""Scheduled run orchestration.

One run = fan out every enabled source across every saved query, dedupe, rank, enrich the
newly-seen postings, merge into the stored corpus, write it back out.

The governing rule is that no single source can fail a run. A blocked Naukri, an expired
Adzuna key and a LinkedIn 429 should all produce a smaller corpus and an honest health
report, never a red workflow and a stale site.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from .config import Config
from .dedupe import dedupe, tier_summary
from .fetcher import make_client
from .models import Job, Query, Seniority, SourceHealth
from .search import rank, score
from .sources import load_all
from .sources.base import build_sources
from .store import Corpus
from .taxonomy import extract_skills
from .textutil import detect_seniority

log = logging.getLogger(__name__)


async def collect(
    queries: list[Query], options: dict[str, Any], *, only: list[str] | None = None
) -> tuple[list[Job], list[SourceHealth]]:
    """Run every source against every query, concurrently."""
    load_all()
    sources = build_sources(options)
    if only:
        sources = [s for s in sources if s.name in only]
    if not sources:
        log.warning("no sources enabled")
        return [], []

    all_jobs: list[Job] = []
    health_by_source: dict[str, SourceHealth] = {}

    # Sources that return a whole feed regardless of the query only need one fetch per run.
    whole_feed = [s for s in sources if s.query_independent]
    per_query = [s for s in sources if not s.query_independent]

    async with make_client() as client:
        if whole_feed:
            # Pass the first query so location/age hints still reach adapters that use them.
            probe = queries[0] if queries else Query()
            results = await asyncio.gather(
                *(src.run(client, probe) for src in whole_feed), return_exceptions=True
            )
            for src, result in zip(whole_feed, results, strict=True):
                if isinstance(result, BaseException):
                    log.error("source %s raised past its handler: %s", src.name, result)
                    continue
                jobs, health = result
                all_jobs.extend(jobs)
                health_by_source[src.name] = health

        for query in queries:
            results = await asyncio.gather(
                *(src.run(client, query) for src in per_query), return_exceptions=True
            )
            for src, result in zip(per_query, results, strict=True):
                if isinstance(result, BaseException):
                    # run() already catches everything; this is belt-and-braces.
                    log.error("source %s raised past its handler: %s", src.name, result)
                    continue
                jobs, health = result
                all_jobs.extend(jobs)

                # Aggregate health across queries: a source is healthy if it worked at
                # least once, and we sum what it returned.
                prior = health_by_source.get(src.name)
                if prior is None:
                    health_by_source[src.name] = health
                else:
                    prior.jobs_found += health.jobs_found
                    prior.duration_ms += health.duration_ms
                    if health.ok and not prior.ok:
                        prior.ok, prior.error = True, ""
                    elif not health.ok and not prior.ok and health.error:
                        prior.error = health.error

    return all_jobs, list(health_by_source.values())


async def run(
    data_dir: Path,
    *,
    config: Config | None = None,
    only: list[str] | None = None,
    dry_run: bool = False,
    use_llm: bool = True,
    rebuild: bool = False,
) -> dict[str, Any]:
    cfg = config or Config()
    queries = cfg.queries()
    if not queries:
        raise ValueError("config/queries.yml defines no queries")

    raw, health = await collect(queries, cfg.source_options(), only=only)
    log.info("collected %d raw postings from %d sources", len(raw), len(health))

    merged = dedupe(raw)
    log.info("deduped %d -> %d", len(raw), len(merged))

    # Keep anything relevant to at least one saved query. Scoring per-query then taking the
    # union avoids letting a broad query drown out a narrow one.
    keep: dict[str, Job] = {}
    for query in queries:
        for job in rank(list(merged), query, min_score=0.02):
            best = keep.get(job.id)
            if best is None or job.score > best.score:
                keep[job.id] = job
    kept = list(keep.values())
    log.info("relevance filter kept %d", len(kept))

    # Vocabulary-based skill and seniority extraction, always. This is deliberately
    # outside the LLM path: `skills` is one of the five fields search matches on, and when
    # it only got populated by enrichment, a corpus built without an API key had it empty
    # on 100% of jobs — which quietly reduced search to title-and-company only.
    for job in kept:
        if not job.skills:
            job.skills = extract_skills(job.title, job.description)
        if job.seniority == Seniority.UNKNOWN:
            job.seniority = detect_seniority(job.title, job.description)

    corpus = Corpus(data_dir)
    # Stored postings survive across runs by design, so tightening a filter in queries.yml
    # would otherwise leave the jobs it was meant to exclude sitting in the corpus until
    # they aged out. --rebuild discards the old corpus and re-derives it from this run.
    existing = {} if rebuild else corpus.load()

    if use_llm:
        # Imported lazily so phase 1 runs with no LLM dependency installed or configured.
        try:
            from .enrich import enrich_new

            new_jobs = [j for j in kept if j.id not in existing or not existing[j.id].enriched]
            await enrich_new(new_jobs)
        except ImportError:
            log.info("enrichment unavailable; continuing without it")
        except Exception as exc:  # noqa: BLE001 - enrichment must never fail a run
            log.warning("enrichment failed, continuing: %s", exc)

    final = corpus.merge(existing, kept, window_days=cfg.window_days)

    # Store a query-independent score so the static frontend has a sane default sort.
    default_query = Query(keywords=[], location="", max_age_days=0, limit=0)
    for job in final:
        job.score = score(job, default_query)

    if dry_run:
        return {
            "dry_run": True,
            "raw": len(raw),
            "deduped": len(merged),
            "kept": len(kept),
            "final": len(final),
            "tiers": tier_summary(final),
            "health": [h.model_dump(mode="json") for h in health],
        }

    meta = corpus.save(final, health, extra_meta={"tiers": tier_summary(final)})
    log.info("wrote corpus: %s", meta)
    return meta
