"""Corpus persistence.

Layout written to the `data` branch and served by Pages:

    index.json          lightweight record per job — the frontend loads this whole file
    jobs/<shard>.json   full descriptions, 2-hex shards, fetched lazily on card open
    health.json         per-source outcome of the latest run
    meta.json           counts and timestamps

The split exists because index.json is downloaded by every visitor on every page load.
Full descriptions would make it tens of megabytes; sharding keeps the first paint small
while still allowing a static host to serve full detail with no backend.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Job, SourceHealth

log = logging.getLogger(__name__)

SHARD_CHARS = 2  # 256 shards — a few hundred KB each at realistic corpus sizes


class Corpus:
    """Read/write the job corpus on disk."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.jobs_dir = self.root / "jobs"

    # ---------------------------------------------------------------- read

    def load(self) -> dict[str, Job]:
        """Load the full corpus (index + shards) keyed by job id."""
        index_path = self.root / "index.json"
        if not index_path.exists():
            return {}

        try:
            entries = json.loads(index_path.read_text())
        except json.JSONDecodeError:
            log.warning("index.json is corrupt; starting from an empty corpus")
            return {}

        # Descriptions live in shards; load them all for the pipeline's merge step.
        details: dict[str, dict] = {}
        if self.jobs_dir.exists():
            for shard in self.jobs_dir.glob("*.json"):
                try:
                    details.update(json.loads(shard.read_text()))
                except json.JSONDecodeError:
                    log.warning("shard %s is corrupt; skipping", shard.name)

        jobs: dict[str, Job] = {}
        for entry in entries if isinstance(entries, list) else entries.get("jobs", []):
            merged = {**entry, **details.get(entry["id"], {})}
            try:
                jobs[entry["id"]] = Job.model_validate(merged)
            except Exception as exc:  # noqa: BLE001 - one bad row must not break a run
                log.debug("dropping unreadable corpus row %s: %s", entry.get("id"), exc)
        return jobs

    # ---------------------------------------------------------------- write

    def save(
        self,
        jobs: list[Job],
        health: list[SourceHealth],
        *,
        extra_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

        # Newest first, so a frontend that renders before sorting still looks right.
        jobs = sorted(jobs, key=lambda j: j.posted_at or j.first_seen_at, reverse=True)

        index = [j.index_entry() for j in jobs]
        (self.root / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, separators=(",", ":"))
        )

        shards: dict[str, dict[str, dict]] = defaultdict(dict)
        for job in jobs:
            shards[job.id[:SHARD_CHARS]][job.id] = {
                "description": job.description,
                "apply_url": job.apply_url,
                "salary_period": job.salary_period,
                "enriched": job.enriched,
            }

        # Remove shards that no longer have any jobs, or stale rows linger forever.
        for old in self.jobs_dir.glob("*.json"):
            if old.stem not in shards:
                old.unlink()
        for name, payload in shards.items():
            (self.jobs_dir / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )

        (self.root / "health.json").write_text(
            json.dumps([h.model_dump(mode="json") for h in health], ensure_ascii=False, indent=1)
        )

        meta = {
            "updated_at": datetime.now(UTC).isoformat(),
            "total_jobs": len(jobs),
            "sources_ok": sum(1 for h in health if h.ok),
            "sources_failed": sum(1 for h in health if not h.ok),
            "by_source": _counts(jobs),
            "enriched": sum(1 for j in jobs if j.enriched),
            **(extra_meta or {}),
        }
        (self.root / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
        return meta

    # ---------------------------------------------------------------- merge

    def merge(
        self, existing: dict[str, Job], incoming: list[Job], *, window_days: int
    ) -> list[Job]:
        """Fold a run's results into the stored corpus and drop anything past the window.

        Preserves `first_seen_at` and any enrichment already paid for, so a re-scrape of an
        unchanged posting costs nothing downstream.
        """
        now = datetime.now(UTC)
        for job in incoming:
            prior = existing.get(job.id)
            if prior:
                job.first_seen_at = prior.first_seen_at
                # Keep LLM output rather than re-spending quota on an unchanged posting.
                if prior.enriched and not job.enriched:
                    job.skills = prior.skills
                    job.seniority = prior.seniority
                    job.summary = prior.summary
                    job.tags = sorted(set(job.tags) | set(prior.tags))[:16]
                    job.enriched = True
                if not job.description and prior.description:
                    job.description = prior.description
            job.last_seen_at = now
            existing[job.id] = job

        cutoff = now - timedelta(days=window_days)
        return [j for j in existing.values() if (j.posted_at or j.first_seen_at) >= cutoff]


def _counts(jobs: list[Job]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for j in jobs:
        out[j.source] += 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
