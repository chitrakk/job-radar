"""Command line entry point.

`jobradar pipeline` is what the scheduled workflow runs. The other commands exist for
debugging a single source without burning a full run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer

from .config import Config, find_config_dir
from .models import Query

app = typer.Typer(add_completion=False, help="Aggregate job postings into a static corpus.")

# CV scoring, outreach drafting, interview prep and the Sheets tracker.
from .career.cli import app as career_app  # noqa: E402

app.add_typer(career_app, name="career")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def _default_data_dir() -> Path:
    return find_config_dir().parent / "data"


@app.command()
def pipeline(
    data_dir: Path = typer.Option(None, help="Where to write the corpus."),
    only: list[str] = typer.Option(None, "--only", help="Restrict to these source names."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Collect and report, write nothing."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip enrichment."),
    rebuild: bool = typer.Option(False, "--rebuild", help="Discard the stored corpus first."),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run the full scheduled pipeline."""
    _setup_logging(verbose)
    from .pipeline import run

    result = asyncio.run(
        run(
            data_dir or _default_data_dir(),
            only=list(only) if only else None,
            dry_run=dry_run,
            use_llm=not no_llm,
            rebuild=rebuild,
        )
    )
    typer.echo(json.dumps(result, indent=2, default=str))


@app.command()
def scrape(
    source: str = typer.Option(..., "--source", help="Source name, e.g. ats:greenhouse."),
    query: list[str] = typer.Option(None, "--query", "-q", help="Keyword (repeatable)."),
    location: str = typer.Option("", "--location"),
    country: str = typer.Option("in", "--country"),
    limit: int = typer.Option(10, "--limit"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Run one source and print what it returns. The fastest way to check a board still works."""
    _setup_logging(verbose)
    from .pipeline import collect

    cfg = Config()
    q = Query(
        keywords=list(query) if query else [],
        location=location,
        country=country,
        limit=limit,
    )
    jobs, health = asyncio.run(collect([q], cfg.source_options(), only=[source]))

    for h in health:
        status = "ok" if h.ok else f"FAILED ({h.error})"
        typer.echo(f"[{h.source}] {status} — {h.jobs_found} jobs in {h.duration_ms}ms")

    for job in jobs[:limit]:
        typer.echo(f"\n  {job.title}\n  {job.company} · {job.location or '—'} · {job.remote.value}")
        if job.salary_min:
            typer.echo(
                f"  salary: {job.salary_currency} {job.salary_min:,.0f}–{job.salary_max:,.0f}"
            )
        typer.echo(f"  {job.url}")


@app.command()
def sources() -> None:
    """List registered sources and whether they are enabled."""
    from .sources import load_all
    from .sources.base import registry

    load_all()
    cfg = Config()
    overrides = cfg.source_options().get("sources", {})
    for name, cls in sorted(registry().items()):
        opts = overrides.get(name, {}) or {}
        enabled = opts.get("enabled", cls.enabled)
        companies = len(opts.get("companies", []) or [])
        extra = f" · {companies} boards" if companies else ""
        typer.echo(f"{'✓' if enabled else '·'} {name:<24} {cls.tier.value}{extra}")


@app.command()
def stats(data_dir: Path = typer.Option(None)) -> None:
    """Summarise the stored corpus."""
    from .store import Corpus

    corpus = Corpus(data_dir or _default_data_dir())
    jobs = corpus.load()
    typer.echo(f"{len(jobs)} jobs in corpus")
    meta_path = corpus.root / "meta.json"
    if meta_path.exists():
        typer.echo(meta_path.read_text())


if __name__ == "__main__":
    app()
