"""CLI for the career tools: CV scoring, outreach drafting, interview prep, tracker."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer

from ..models import Job
from ..store import Corpus

app = typer.Typer(add_completion=False, help="CV, outreach and interview tools.")


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )


def _data_dir(explicit: Path | None) -> Path:
    from ..config import find_config_dir

    return explicit or find_config_dir().parent / "data"


def _load_job(job_id: str, data_dir: Path | None) -> Job:
    corpus = Corpus(_data_dir(data_dir))
    jobs = corpus.load()
    if job_id in jobs:
        return jobs[job_id]
    # Allow an id prefix, since full hashes are tedious to type.
    matches = [j for jid, j in jobs.items() if jid.startswith(job_id)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise typer.BadParameter(
            f"no job with id {job_id!r} in the corpus. Run `jobradar pipeline` first, "
            "or use `jobradar career find` to look one up."
        )
    raise typer.BadParameter(f"{job_id!r} matches {len(matches)} jobs; use a longer id")


@app.command("score-cv")
def score_cv_cmd(
    cv: Path = typer.Argument(..., help="Path to your CV (.txt, .md or .pdf)."),
    job_id: str = typer.Option("", "--job", help="Score against a specific posting."),
    job_file: Path = typer.Option(None, "--jd-file", help="Score against a JD text file."),
    data_dir: Path = typer.Option(None, "--data-dir"),
    to_sheet: bool = typer.Option(False, "--to-sheet", help="Log the score to the tracker."),
    as_json: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Score a CV out of 100 and get grounded rewrite suggestions."""
    _log(verbose)
    from . import cv as cvmod

    cv_text = cvmod.read_cv(cv)

    jd = ""
    target = ""
    if job_id:
        job = _load_job(job_id, data_dir)
        jd = job.description
        target = f"{job.title} at {job.company}"
    elif job_file:
        jd = Path(job_file).read_text(encoding="utf-8", errors="replace")
        target = job_file.name

    report = asyncio.run(cvmod.score_cv(cv_text, job_description=jd))

    if as_json:
        typer.echo(json.dumps(report, indent=2, default=str))
    else:
        if target:
            typer.echo(f"Scored against: {target}\n")
        typer.echo(cvmod.render_report(report))

    if to_sheet:
        from . import sheets

        try:
            result = asyncio.run(sheets.push("cv", [sheets.cv_row(report, target_role=target)]))
        except sheets.SheetsError as exc:
            typer.secho(f"tracker: {exc}", fg="yellow")
        else:
            typer.echo(f"tracker: logged to {result.get('tab')}")


@app.command()
def outreach(
    job_id: str = typer.Argument(..., help="Job id (or unique prefix) from the corpus."),
    cv: Path = typer.Option(..., "--cv", help="Your CV — the only source for claims."),
    name: str = typer.Option("", "--name", help="Your name, for the sign-off."),
    data_dir: Path = typer.Option(None, "--data-dir"),
    no_research: bool = typer.Option(False, "--no-research", help="Skip company lookup."),
    to_sheet: bool = typer.Option(False, "--to-sheet"),
    as_json: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Find who to contact and draft a LinkedIn note plus an email.

    Drafts only — nothing is sent, and no logged-in LinkedIn session is touched.
    """
    _log(verbose)
    from . import cv as cvmod
    from . import outreach as omod

    job = _load_job(job_id, data_dir)
    cv_text = cvmod.read_cv(cv)

    draft = asyncio.run(
        omod.draft_outreach(
            job,
            candidate_summary=cv_text,
            candidate_name=name,
            research=not no_research,
        )
    )

    if as_json:
        typer.echo(json.dumps(draft.as_dict(), indent=2, default=str))
        return

    typer.secho(f"\n{job.title} — {job.company}", bold=True)
    if draft.company_domain:
        typer.echo(f"domain: {draft.company_domain}")

    typer.secho("\nCONTACTS", bold=True)
    for c in draft.contacts:
        label = c.name or "(no name published)"
        typer.echo(f"  {label} — {c.role}")
        if c.email:
            typer.echo(f"    email: {c.email}  [{c.email_confidence}]")
        if c.email_basis:
            typer.echo(f"    basis: {c.email_basis}")
        if c.linkedin_search_url:
            typer.echo(f"    find:  {c.linkedin_search_url}")
        if c.source:
            typer.echo(f"    from:  {c.source}")

    if draft.linkedin_note:
        typer.secho(f"\nLINKEDIN NOTE ({len(draft.linkedin_note)}/300 chars)", bold=True)
        typer.echo(f"  {draft.linkedin_note}")

    if draft.email_body:
        typer.secho("\nEMAIL", bold=True)
        typer.echo(f"  Subject: {draft.email_subject}")
        typer.echo("")
        for line in draft.email_body.splitlines():
            typer.echo(f"  {line}")

    slop = draft.slop or {}
    flagged = (slop.get("linkedin_note") or []) + (slop.get("email_body") or [])
    if flagged:
        typer.secho("\nSLOP CHECK — fix before sending", fg="yellow", bold=True)
        for h in flagged:
            typer.echo(f"  [{h['severity']}] {h['label']}: {h['excerpt']!r}")
            typer.echo(f"     → {h['fix']}")
    elif draft.linkedin_note:
        typer.secho("\nSLOP CHECK — clean", fg="green")

    for w in draft.warnings:
        typer.secho(f"\nnote: {w}", fg="yellow")

    typer.secho(
        "\nNothing has been sent. Review, edit, and send it yourself — automating "
        "LinkedIn outreach risks a permanent account restriction.",
        fg="cyan",
    )

    if to_sheet:
        from . import sheets

        named = next((c for c in draft.contacts if c.name), draft.contacts[0])
        try:
            asyncio.run(sheets.push("outreach", [sheets.outreach_row(job, draft, named)]))
            asyncio.run(
                sheets.push(
                    "applications", [sheets.application_row(job, status="Outreach drafted")]
                )
            )
        except sheets.SheetsError as exc:
            typer.secho(f"tracker: {exc}", fg="yellow")
        else:
            typer.echo("tracker: logged")


@app.command()
def interview(
    job_id: str = typer.Argument(...),
    cv: Path = typer.Option(..., "--cv"),
    data_dir: Path = typer.Option(None, "--data-dir"),
    as_json: bool = typer.Option(False, "--json"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Build an interview prep pack for one posting, grounded in your CV."""
    _log(verbose)
    from . import cv as cvmod
    from . import interview as imod

    job = _load_job(job_id, data_dir)
    pack = asyncio.run(imod.prep(job, cv_text=cvmod.read_cv(cv)))

    if as_json:
        typer.echo(json.dumps(pack, indent=2, default=str))
        return
    typer.secho(f"\n{job.title} — {job.company}\n", bold=True)
    typer.echo(imod.render(pack))


@app.command()
def find(
    query: str = typer.Argument("", help="Match against title or company."),
    limit: int = typer.Option(15, "--limit"),
    data_dir: Path = typer.Option(None, "--data-dir"),
) -> None:
    """List corpus jobs with their ids, so you can pass one to the other commands."""
    corpus = Corpus(_data_dir(data_dir))
    jobs = list(corpus.load().values())
    q = query.lower()
    if q:
        jobs = [j for j in jobs if q in j.title.lower() or q in j.company.lower()]
    jobs.sort(key=lambda j: j.posted_at or j.first_seen_at, reverse=True)

    if not jobs:
        typer.echo("no matching jobs in the corpus")
        return
    for job in jobs[:limit]:
        typer.echo(
            f"{job.id[:10]}  {job.title[:44]:<44} {job.company[:22]:<22} {job.location[:22]}"
        )


@app.command()
def track(
    job_id: str = typer.Argument(...),
    status: str = typer.Option("Applied", "--status"),
    notes: str = typer.Option("", "--notes"),
    data_dir: Path = typer.Option(None, "--data-dir"),
) -> None:
    """Log an application to the Google Sheet tracker."""
    from . import sheets

    job = _load_job(job_id, data_dir)
    try:
        result = asyncio.run(
            sheets.push("applications", [sheets.application_row(job, status=status, notes=notes)])
        )
    except sheets.SheetsError as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(1) from exc
    typer.echo(f"{job.title} @ {job.company} → {result.get('tab')} ({status})")


@app.command("check-text")
def check_text(
    path: Path = typer.Argument(None, help="File to check; omit to read stdin."),
    kind: str = typer.Option("cv", "--kind", help="cv | linkedin_note | email"),
) -> None:
    """Run the AI-slop check on any text. No LLM needed — pure pattern matching."""
    import sys

    from .slop import clean_report

    text = path.read_text(encoding="utf-8", errors="replace") if path else sys.stdin.read()
    report = clean_report(text, kind=kind)

    if report["clean"]:
        typer.secho("clean — no slop patterns found", fg="green")
        return
    typer.secho(f"{report['count']} issues {report['by_severity']}", fg="yellow", bold=True)
    for h in report["hits"]:
        typer.echo(f"  [{h['severity']:<6}] {h['label']}")
        typer.echo(f"             {h['excerpt']!r}")
        typer.echo(f"          → {h['fix']}")
